"""The click signer's passkey step-up check (host spec §0.8.6 item 10.5; #1198 R1-6).

On a remote dashboard session the listed ops (turning One click on, a sealed new passphrase at One click off, a new
device key, dashboard execution on, a hub step or a shown grant secret at sign) need a passkey assertion. The dashboard
server relays it, but THIS module is the check that counts: the signer draws the nonce itself (``begin``), keeps it in
memory only, and ``check`` accepts an assertion only for a nonce it issued for this workspace and this digest, once,
within ``NONCE_LIFETIME``, signed by a passkey it reads itself from the person's store. So a server bug, or anyone who
only drives the server's HTTP workflow, cannot make the signer skip it: "the server says it is fine" is not an input.

The store is read-only here (the same checks as the server's: own file, mode 0600, strict shape), the relying party is
read from ``<state>/dashboard/remote.json`` (the name `orch remote enable` wrote), and nothing here writes a file.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import passkeys as wa
from .passkey_store import PasskeyStore, StoreError
from .protocol import ClickError

__all__ = ["CODE", "NONCE_LIFETIME", "Stepups"]

CODE = "passkey.stepup_required"
#: a nonce can be answered by the server within 120 s (its ceremony) and the answer used for the op for at most the
#: ten minutes of a fresh step-up (owner decision of 24 Oct 2026; also a request's own lifetime)
NONCE_LIFETIME = 600.0
MAX_NONCES = 64
_REMOTE_FILE = "remote.json"
_RESPONSE = {"clientDataJSON", "authenticatorData", "signature"}


def _refuse(why: str) -> ClickError:
    # one static text: nothing tells the caller which part failed (the reason is the signer's log line only)
    return ClickError(CODE, "This needs a fresh fingerprint or PIN check first.", reason=None)


class Stepups:
    """``rp_id``/``store`` are seams for tests; by default read from the state directory."""

    def __init__(
        self,
        state: Path,
        mono: Callable[[], float],
        *,
        rp_id: Callable[[], str | None] | None = None,
        store: PasskeyStore | None = None,
    ) -> None:
        self.dir = Path(state) / "dashboard"
        self.mono = mono
        self._rp_id = rp_id or self._read_rp_id
        self.store = store or PasskeyStore(self.dir / "passkeys.json")
        #: nonce (raw bytes) -> (workspace id, view digest, op, request id or None, issued at); in memory only, a
        #: restart forgets all
        self.nonces: dict[bytes, tuple[str, str, str, str | None, float]] = {}

    def _read_rp_id(self) -> str | None:
        """The configured web name, or None (remote access off, or a file that cannot be trusted)."""
        try:
            fd = os.open(self.dir / _REMOTE_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError:
            return None
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_size > 4096:
                return None
            raw = os.read(fd, 4097)
        finally:
            os.close(fd)
        try:
            name = json.loads(raw.decode("utf-8")).get("name")
        except (ValueError, AttributeError):
            return None
        return name if isinstance(name, str) else None

    def begin(self, workspace_id: str, digest: str, op: str, request_id: str | None = None) -> str:
        """A nonce for one step-up of ``op`` (and ``request_id`` where the call has one) whose digest is ``digest`` in
        ``workspace_id``."""
        if not isinstance(digest, str):
            raise ClickError("invalid.input", "view_digest is a digest")
        wa.stepup_challenge(bytes(wa.CHALLENGE_BYTES), digest)  # shape check only
        now = self.mono()
        self.nonces = {n: v for n, v in self.nonces.items() if now - v[4] < NONCE_LIFETIME}
        while len(self.nonces) >= MAX_NONCES:
            del self.nonces[next(iter(self.nonces))]
        nonce = secrets.token_bytes(wa.CHALLENGE_BYTES)
        self.nonces[nonce] = (workspace_id, digest, op, request_id, now)
        return wa.b64u_encode(nonce)

    def check(self, workspace_id: str, digest: str, op: str, request_id: str | None, proof: Any) -> None:
        """Raise ``passkey.stepup_required`` unless ``proof`` is a valid assertion for a nonce this signer issued for
        exactly (``workspace_id``, ``digest``, ``op``, ``request_id``). The nonce is used up first, whatever the
        outcome."""
        try:
            nonce_text = proof["nonce"] if isinstance(proof, dict) else None
            nonce = wa.b64u_decode(nonce_text, wa.CHALLENGE_BYTES) if isinstance(nonce_text, str) else b""
        except wa.PasskeyError:
            nonce = b""
        issued = self.nonces.pop(nonce, None)  # used up before anything else
        if issued is None:
            raise _refuse("unknown or used nonce")
        wid, bound, bound_op, bound_request, at = issued
        if (wid, bound, bound_op, bound_request) != (
            workspace_id,
            digest,
            op,
            request_id,
        ) or self.mono() - at >= NONCE_LIFETIME:
            raise _refuse("nonce for another op, or too old")
        rp_id = self._rp_id()
        if rp_id is None:
            raise _refuse("remote access is not configured")
        try:
            if set(proof) != {"nonce", "id", "response"} or not isinstance(proof["response"], dict):
                raise _refuse("shape")
            response = proof["response"]
            if not _RESPONSE <= set(response) <= _RESPONSE | {"userHandle"}:
                raise _refuse("shape")
            credential_id = wa.b64u_decode(proof["id"], wa.MAX_CREDENTIAL_ID)
            record = self.store.find(credential_id)
            if record is None:
                raise _refuse("unknown credential")
            handle = response.get("userHandle")
            wa.verify_assertion(
                credential_id=credential_id,
                client_data_json=wa.b64u_decode(response["clientDataJSON"], wa.MAX_CLIENT_DATA),
                authenticator_data=wa.b64u_decode(response["authenticatorData"], wa.MAX_AUTH_DATA),
                signature=wa.b64u_decode(response["signature"], wa.MAX_SIGNATURE),
                user_handle=wa.b64u_decode(handle, wa.MAX_USER_HANDLE) if handle else None,
                public_key=record.key_bytes,
                expected_user_handle=record.handle_bytes,
                expected_backup_eligible=record.backup_eligible,
                challenge=wa.stepup_challenge(nonce, digest),
                rp_id=rp_id,
                origin="https://" + rp_id,
            )
        except (wa.PasskeyError, StoreError, TypeError, KeyError, AttributeError):
            raise _refuse("assertion") from None
