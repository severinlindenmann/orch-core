"""The platform-authenticator binding (spec §9): challenge bytes, registration inside the pairing ceremony and
assertion verification. Pure functions; the caller keeps the open challenges (single use) and the credential."""
from __future__ import annotations

import hashlib
import json
import struct
from dataclasses import dataclass

from orch.remote.bridge_host.envelope import unb64u
from orch.remote.bridge_host.registry import SCOPES, Credential
from orch.remote.bridge_host.shown import subject_hash
from orch.remote.bridge_host.signatures import load_public, public_bytes, verify_der

L_ASSERT = b"sharing/bridge/assert/v1|"
L_REG = b"sharing/bridge/webauthn-reg/v1|"
PURPOSES = {"fresh": 1, "lease": 2}
CHALLENGE_MS = 120_000
AD_UP, AD_UV, AD_BE, AD_BS, AD_AT, AD_ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80
LEASE_SUBJECT = {"kind": "lease", "shown": "Type for 15 minutes", "digest": ""}
SUBJECT_KINDS = ("action", "command", "charter", "verdict", "permission", "lease")


def assertion_challenge(workspace: bytes, device: bytes, rid: bytes, purpose: str, scope: str, expires_ms: int,
                        nonce: bytes, subject: dict) -> bytes:
    if len(workspace) != 16 or len(device) != 16 or len(rid) != 16 or len(nonce) != 32:
        raise ValueError("challenge input of the wrong length")
    return hashlib.sha256(L_ASSERT + workspace + device + rid + bytes([PURPOSES[purpose], SCOPES[scope]])
                          + struct.pack(">Q", expires_ms) + nonce + subject_hash(subject)).digest()


def registration_challenge(workspace: bytes, device: bytes, expires_ms: int, nonce: bytes) -> bytes:
    if len(workspace) != 16 or len(device) != 16 or len(nonce) != 32:
        raise ValueError("challenge input of the wrong length")
    return hashlib.sha256(L_REG + workspace + device + struct.pack(">Q", expires_ms) + nonce).digest()


@dataclass(frozen=True)
class Issued:
    """An open assertion challenge: who it was issued to and what it allows."""
    device: str  # hex
    expires_ms: int
    rid: str = ""  # the request R1 it allows (hex)
    purpose: str = "fresh"
    scope: str = "type"
    subject: dict | None = None


@dataclass(frozen=True)
class Verified:
    ok: bool
    why: str | None = None  # host-side reason of a failure, for the audit log; never sent
    counter_warning: bool = False  # the count did not increase on a synced credential: advisory, shown
    sign_count: int | None = None  # the count to store, None to keep the stored one
    issued: Issued | None = None


def _fail(why: str, issued: Issued | None = None) -> Verified:
    return Verified(False, why, issued=issued)


def _client(cdj: bytes) -> dict:
    client = json.loads(cdj.decode("utf-8"))
    if not isinstance(client, dict):
        raise ValueError("client data is not an object")
    return client


def verify_assertion(cred: Credential, cred_device: str, pending: dict, sender: str, credential_id: bytes,
                     authenticator_data: bytes, client_data_json: bytes, signature: bytes, now_ms: int) -> Verified:
    """§9.5, in order, failing closed. `pending` maps challenge hex to Issued; the challenge named by the client data
    is removed from it whatever happens next (single use)."""
    ad = authenticator_data
    try:
        client = _client(client_data_json)
        ch = unb64u(client["challenge"]).hex()
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return _fail("malformed")
    issued = pending.pop(ch, None)
    if issued is None:
        return _fail("unknown_or_used_challenge")
    if now_ms >= issued.expires_ms:
        return _fail("expired", issued)
    if issued.device != sender or cred_device != sender:
        return _fail("other_device", issued)
    if credential_id != cred.credential_id:
        return _fail("other_credential", issued)
    if (client.get("type") != "webauthn.get" or client.get("origin") != cred.origin
            or client.get("crossOrigin") not in (None, False)):
        return _fail("client_data", issued)
    if len(ad) < 37 or ad[:32] != hashlib.sha256(cred.rp_id.encode("utf-8")).digest():
        return _fail("rp_id", issued)
    if ad[32] & (AD_UP | AD_UV) != AD_UP | AD_UV:
        return _fail("user_not_verified", issued)
    if bool(ad[32] & AD_BE) != cred.be:
        return _fail("backup_eligibility_changed", issued)
    if not verify_der(cred.pub, signature, ad + hashlib.sha256(client_data_json).digest()):
        return _fail("signature", issued)
    count = struct.unpack(">I", ad[33:37])[0]
    if (count or cred.sign_count) and count <= cred.sign_count:
        if not cred.be:
            return _fail("sign_count", issued)  # this assertion only: shown, the credential is not suspended (D5)
        return Verified(True, counter_warning=True, issued=issued)  # synced: advisory, the stored count is kept
    return Verified(True, sign_count=count, issued=issued)


# -- registration (§9.2): a minimal CBOR reader for the attestation object and the COSE key --------------------------

class _Cbor:
    """Definite-length CBOR: integers, byte and text strings, arrays, maps, false/true/null. No tags, no floats, no
    indefinite lengths, no duplicate map keys; bounded depth."""

    def __init__(self, raw: bytes):
        self.raw, self.at = raw, 0

    def _take(self, n: int) -> bytes:
        if n < 0 or self.at + n > len(self.raw):
            raise ValueError("CBOR runs past the end")
        out = self.raw[self.at:self.at + n]
        self.at += n
        return out

    def _arg(self, info: int) -> int:
        if info < 24:
            return info
        if info in (24, 25, 26, 27):
            return int.from_bytes(self._take(1 << (info - 24)), "big")
        raise ValueError("unsupported CBOR length")

    def item(self, depth: int = 0):
        if depth > 8:
            raise ValueError("CBOR nested too deep")
        first = self._take(1)[0]
        major, info = first >> 5, first & 0x1F
        if major == 7:
            simple = {20: False, 21: True, 22: None}
            if info not in simple:
                raise ValueError("unsupported CBOR simple value")
            return simple[info]
        n = self._arg(info)
        if major == 0:
            return n
        if major == 1:
            return -1 - n
        if major == 2:
            return self._take(n)
        if major == 3:
            return self._take(n).decode("utf-8")
        if major == 4:
            if n > 64:
                raise ValueError("CBOR array too long")
            return [self.item(depth + 1) for _ in range(n)]
        if major == 5:
            if n > 64:
                raise ValueError("CBOR map too long")
            out: dict = {}
            for _ in range(n):
                k = self.item(depth + 1)
                if not isinstance(k, (int, str)) or isinstance(k, bool) or k in out:
                    raise ValueError("bad CBOR map key")
                out[k] = self.item(depth + 1)
            return out
        raise ValueError("unsupported CBOR type")


def _cose_p256(key) -> bytes:
    """A COSE EC2 key on P-256 for ES256 as a 65-byte point; ValueError for anything else or a point off the curve."""
    if not isinstance(key, dict) or key.get(1) != 2 or key.get(3) != -7 or key.get(-1) != 1:
        raise ValueError("not an EC2 P-256 ES256 key")
    x, y = key.get(-2), key.get(-3)
    if not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
        raise ValueError("bad EC2 coordinates")
    return public_bytes(load_public(b"\x04" + x + y))


def verify_registration(expected_challenge: bytes, credential_id: bytes, attestation_object: bytes,
                        client_data_json: bytes, rp_id: str, origin: str) -> tuple[Credential | None, str | None]:
    """§9.2 step 3 for a challenge that is open and was issued to the sending device (the caller checks and removes
    it first): (the credential to store with the pending pairing, None) or (None, the reason). Attestation is none:
    the statement is not checked, only the authenticator data."""
    try:
        client = _client(client_data_json)
        if (client.get("type") != "webauthn.create" or unb64u(client.get("challenge")) != expected_challenge
                or client.get("origin") != origin or client.get("crossOrigin") not in (None, False)):
            return None, "client_data"
        reader = _Cbor(attestation_object)
        att = reader.item()
        if reader.at != len(attestation_object) or not isinstance(att, dict) or not isinstance(att.get("authData"), bytes):
            return None, "attestation_object"
        ad = att["authData"]
        if len(ad) < 37 + 18 or ad[:32] != hashlib.sha256(rp_id.encode("utf-8")).digest():
            return None, "rp_id"
        flags = ad[32]
        if flags & (AD_UP | AD_UV | AD_AT) != AD_UP | AD_UV | AD_AT:
            return None, "flags"
        if flags & AD_BS and not flags & AD_BE:
            return None, "flags"
        count = struct.unpack(">I", ad[33:37])[0]
        n = int.from_bytes(ad[53:55], "big")
        cid = ad[55:55 + n]
        if n == 0 or n > 1023 or len(cid) != n or cid != credential_id:
            return None, "credential_id"
        key_reader = _Cbor(ad[55 + n:])
        pub = _cose_p256(key_reader.item())
        if not flags & AD_ED and key_reader.at != len(ad) - 55 - n:
            return None, "attestation_object"
    except (KeyError, TypeError, ValueError, UnicodeDecodeError):
        return None, "attestation_object"
    return Credential(credential_id, pub, count, bool(flags & AD_BE), bool(flags & AD_BS), rp_id, origin), None
