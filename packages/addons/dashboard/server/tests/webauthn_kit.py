"""A software authenticator for the passkey route tests: makes the registration and assertion responses a browser would
send, for a chosen RP, with its own CBOR encoder (independent of `orch.click.passkeys`)."""

import hashlib
import json
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from vectors import make_webauthn_vectors as mv

b64u = mv.b64u
UP, UV, BE, AT = mv.UP, mv.UV, mv.BE, mv.AT


class Authenticator:
    def __init__(self, rp_id, *, user_handle=None, uv=True, backup_eligible=False):
        self.rp_id, self.origin = rp_id, "https://" + rp_id
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.credential_id = secrets.token_bytes(32)
        self.user_handle = user_handle
        self.flags = UP | (UV if uv else 0) | (BE if backup_eligible else 0)
        self.count = 0

    def _client_data(self, kind, challenge_b64, origin=None):
        return json.dumps(
            {"type": kind, "challenge": challenge_b64, "origin": origin or self.origin, "crossOrigin": False},
            separators=(",", ":"),
        ).encode()

    def create(self, options, origin=None):
        """The registration body fields for the ``publicKey`` options the server sent."""
        self.user_handle = mv.base64.urlsafe_b64decode(options["user"]["id"] + "=" * (-len(options["user"]["id"]) % 4))
        cd = self._client_data("webauthn.create", options["challenge"], origin)
        auth_data = mv.auth_head(self.flags | AT, 0, self.rp_id) + mv.attested(
            self.credential_id, mv.cbor(mv.cose_es(self.key))
        )
        return {
            "id": b64u(self.credential_id),
            "response": {"clientDataJSON": b64u(cd), "attestationObject": b64u(mv.att_object(auth_data))},
        }

    def get(self, options, origin=None, *, handle=True):
        cd = self._client_data("webauthn.get", options["challenge"], origin)
        self.count += 1
        auth_data = mv.auth_head(self.flags, self.count, self.rp_id)
        sig = self.key.sign(auth_data + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64u(self.credential_id),
            "response": {
                "clientDataJSON": b64u(cd),
                "authenticatorData": b64u(auth_data),
                "signature": b64u(sig),
                "userHandle": b64u(self.user_handle) if handle and self.user_handle else None,
            },
        }
