"""The Secure Enclave presence key through the small Swift helper (R5 design 2.3; ``packages/remote-key/README.md``).

One JSON request on the helper's stdin, one JSON reply on its stdout, base64url without padding:
``create`` -> ``{pub, ref}``; ``sign`` (ref, msg, reason) -> ``{sig}``; ``delete``. The helper is started by the
absolute
path of the install record, never through ``PATH``, and it enforces the same domain limit (label + exact length) that
:func:`orch.remoteclient.presence.check_signable` does here first, so a prompt never appears for anything else.

Without a Secure Enclave the helper says ``unavailable`` and there is **no software fallback**.

This build runs the helper UNBOUND (no keychain access group until the Developer ID exists): ``bound`` is False and the
client shows "Not protected by the orch signature" in Technical details."""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u

from .codes import ClientError
from .presence import Prompt, check_signable
from .state import refuse_key_file

__all__ = ["SecureEnclaveBackend"]

_REF = re.compile(r"[0-9a-f]{32}")
TIMEOUT_S = 180  # a person has to touch the sensor


def _run(argv: list[str], stdin: bytes, timeout: float) -> bytes:
    p = subprocess.run(argv, input=stdin, capture_output=True, timeout=timeout, check=False)  # noqa: S603
    return p.stdout


class SecureEnclaveBackend:
    kind = "platform"

    def __init__(
        self,
        helper: str | os.PathLike[str],
        *,
        store: str | os.PathLike[str] | None = None,
        bound: bool = False,
        runner: Callable[[list[str], bytes, float], bytes] = _run,
    ) -> None:
        self.helper = Path(helper)
        self.bound = bound
        self._store = None if store is None else str(store)
        self._runner = runner
        if not self.helper.is_absolute():
            raise ClientError("remote_client.helper", "the helper is started by an absolute path")

    def available(self) -> bool:
        try:
            st = self.helper.stat()
        except OSError:
            return False
        return self.helper.is_file() and bool(st.st_mode & 0o111)

    def _call(self, req: dict[str, Any], timeout: float = TIMEOUT_S) -> dict[str, Any]:
        if not self.available():
            raise ClientError("remote_client.helper")
        argv = [str(self.helper)] + (["--store", self._store] if self._store else [])
        try:
            out = self._runner(argv, json.dumps(req, separators=(",", ":")).encode(), timeout)
            reply = json.loads(out)
        except (OSError, subprocess.SubprocessError, ValueError):
            raise ClientError("remote_client.helper") from None
        if type(reply) is not dict:
            raise ClientError("remote_client.helper")
        err = reply.get("error")
        if err is not None:
            if err == "cancelled":
                raise ClientError("remote_client.cancelled")
            if err == "unavailable":
                raise ClientError("remote_client.no_presence", str(reply.get("detail", ""))[:64])
            raise ClientError("remote_client.helper", str(err)[:32])
        return reply

    def create(self, name: str) -> dict[str, Any]:
        reply = self._call({"op": "create", "label": name.encode()[:64].decode("utf-8", "ignore")})
        if set(reply) != {"pub", "ref"}:
            raise ClientError("remote_client.helper")
        try:
            pub = unb64u(reply["pub"], 65)
            suite.validate_public_key(pub)
        except (EncodingError, Exception):  # noqa: BLE001 - any invalid point
            raise ClientError("remote_client.helper") from None
        ref = reply["ref"]
        if type(ref) is not str or not _REF.fullmatch(ref):
            raise ClientError("remote_client.helper")
        refuse_key_file(ref)
        return {"kind": "platform", "pub65": pub, "ref": ref}

    def assert_(self, ref: str, msg: bytes, prompt: Prompt) -> dict[str, Any]:
        refuse_key_file(ref)
        if type(ref) is not str or not _REF.fullmatch(ref):
            raise ClientError("remote_client.key_file_refused")
        check_signable(msg)  # before any helper process, before any prompt
        reply = self._call({"op": "sign", "ref": ref, "msg": b64u(msg), "reason": prompt.reason})
        if set(reply) != {"sig"}:
            raise ClientError("remote_client.helper")
        try:
            sig = unb64u(reply["sig"], 64)
        except EncodingError:
            raise ClientError("remote_client.helper") from None
        return {"form": "p256", "sig": b64u(sig)}

    def delete(self, ref: str) -> None:
        if type(ref) is str and _REF.fullmatch(ref):
            self._call({"op": "delete", "ref": ref}, 30)
