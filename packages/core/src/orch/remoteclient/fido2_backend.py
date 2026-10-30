"""The FIDO2 presence key (R5 design 2.4; hub contract 10.2c "Presence", vectors ``presence_*`` ctap2 form).

Plain CTAP2 ``makeCredential`` (``rk=false``, ES256 only, rpId ``orch-remote``) and ``getAssertion`` with
``clientDataHash = SHA-256(msg)``. The checks the HOST makes are made here first, so a bad answer is never sent:

* before enrollment: the authenticator must be able to verify the user (built-in UV, or a PIN set) and speak ES256 --
  "This security key has no PIN or fingerprint. Set a PIN first."; a touch alone (UP only) is not a presence;
* on every assertion: exactly 37 bytes of ``authData``, the rpIdHash, **UP and UV** set, BE and BS clear (a synced
passkey
  is refused), no attested-credential or extension data, a signature that verifies under the stored key, and a counter
  above the held one whenever either is non-zero (the counter is kept per credential in the state dir).

The device is a seam (:class:`Authenticator`); :class:`HidAuthenticator` is the python-fido2 implementation over USB
HID. Every rule above lives in :class:`Fido2Backend` and is tested with a fake authenticator."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from orch.crypto import suite
from orch.crypto.encoding import EncodingError, b64u, unb64u

from .codes import ClientError
from .presence import Prompt, check_signable
from .state import Store

__all__ = ["Authenticator", "Fido2Backend", "HidAuthenticator"]

RP_ID = "orch-remote"
UP, UV, BE, BS, AT, ED = 0x01, 0x04, 0x08, 0x10, 0x40, 0x80
ES256 = -7


class Authenticator(Protocol):
    def info(self) -> dict[str, Any]:
        """``{"options": {"uv": bool|None, "clientPin": bool|None, ...}, "algorithms": [-7, ...] | None}``."""

    def make_credential(self, client_data_hash: bytes, user_name: str, prompt: Prompt) -> dict[str, Any]:
        """``{"cred_id": bytes, "x": bytes(32), "y": bytes(32), "alg": int, "flags": int}`` (``flags`` = authData
        byte 32)."""

    def get_assertion(self, client_data_hash: bytes, cred_id: bytes, prompt: Prompt) -> dict[str, Any]:
        """``{"auth_data": bytes, "sig": bytes}`` (``sig`` DER, as CTAP2 gives it)."""


def _raw_sig(der: bytes) -> bytes:
    try:
        r, s = decode_dss_signature(der)
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    except (ValueError, OverflowError):
        raise ClientError("remote_client.assertion") from None


def _ref(cred_id: bytes) -> str:
    return "fido2-" + hashlib.sha256(cred_id).digest()[:16].hex()


class Fido2Backend:
    kind = "fido2"

    def __init__(self, authenticator: Authenticator, store: Store) -> None:
        self.auth, self.store = authenticator, store

    # -- enrollment --

    def create(self, name: str) -> dict[str, Any]:
        info = self.auth.info()
        opts = info.get("options") or {}
        if not (opts.get("uv") is True or opts.get("clientPin") is True):
            raise ClientError("remote_client.fido2_no_uv")
        algs = info.get("algorithms")
        if algs is not None and ES256 not in algs:
            raise ClientError("remote_client.fido2_algorithm")
        prompt = Prompt("Add this computer", reason="Add this computer to your workspace")
        made = self.auth.make_credential(hashlib.sha256(os.urandom(32)).digest(), name[:64] or "orch", prompt)
        try:
            cred_id, x, y = made["cred_id"], made["x"], made["y"]
            flags, alg = int(made["flags"]), made["alg"]
        except (KeyError, TypeError, ValueError):
            raise ClientError("remote_client.assertion") from None
        if alg != ES256:
            raise ClientError("remote_client.fido2_algorithm")
        if (
            type(cred_id) is not bytes
            or not 16 <= len(cred_id) <= 1023
            or len(x) != 32
            or len(y) != 32
            or flags & (UP | UV) != (UP | UV)
            or flags & (BE | BS)
        ):
            raise ClientError("remote_client.fido2_no_uv" if not flags & UV else "remote_client.assertion")
        pub = b"\x04" + x + y
        try:
            suite.validate_public_key(pub)
        except Exception:  # noqa: BLE001
            raise ClientError("remote_client.assertion") from None
        ref = _ref(cred_id)
        self.store.set_device(ref, {"cred_id": b64u(cred_id), "pub": b64u(pub), "counter": 0})
        return {"kind": "fido2", "pub65": pub, "ref": ref}

    # -- every assertion --

    def assert_(self, ref: str, msg: bytes, prompt: Prompt) -> dict[str, Any]:
        check_signable(msg)  # label + exact length, before any touch
        entry = self.store.devices().get(ref)
        if type(ref) is not str or not ref.startswith("fido2-") or type(entry) is not dict:
            raise ClientError("remote_client.key_file_refused")
        try:
            cred_id, pub = unb64u(entry["cred_id"]), unb64u(entry["pub"], 65)
        except (KeyError, EncodingError):
            raise ClientError("remote_client.state") from None
        cdh = hashlib.sha256(msg).digest()
        got = self.auth.get_assertion(cdh, cred_id, prompt)
        try:
            auth_data, sig = got["auth_data"], _raw_sig(got["sig"])
        except (KeyError, TypeError):
            raise ClientError("remote_client.assertion") from None
        if type(auth_data) is not bytes or len(auth_data) != 37:
            raise ClientError("remote_client.assertion")  # attested data or extensions: the host refuses them too
        flags, count = auth_data[32], int.from_bytes(auth_data[33:37], "big")
        held = int(entry.get("counter", 0)) if type(entry.get("counter", 0)) is int else 0
        if (
            auth_data[:32] != hashlib.sha256(RP_ID.encode()).digest()
            or flags & (AT | ED)
            or flags & (UP | UV) != (UP | UV)  # a touch without user verification is not a presence
            or flags & (BE | BS)  # a synced passkey is not a key that stays on one device
            or ((held > 0 or count > 0) and count <= held)  # the counter must rise
            or not suite.verify(pub, sig, auth_data + cdh)
        ):
            raise ClientError("remote_client.assertion")
        self.store.set_counter(ref, count)
        return {"form": "ctap2", "auth_data": b64u(auth_data), "sig": b64u(sig)}


class HidAuthenticator:
    """A CTAP2 security key over USB HID (python-fido2). ``pin`` returns the PIN when the key needs one (the UI asks
    the person; it is never stored). Not exercised in CI (no hardware): the owner checks it with a key on Linux."""

    def __init__(self, pin: Callable[[], str | None] | None = None, device: Any = None) -> None:
        self._pin, self._device = pin, device

    def _ctap(self) -> Any:
        from fido2.ctap2 import Ctap2
        from fido2.hid import CtapHidDevice

        dev = self._device or next(iter(CtapHidDevice.list_devices()), None)
        if dev is None:
            raise ClientError("remote_client.no_presence")
        return Ctap2(dev)

    def info(self) -> dict[str, Any]:
        i = self._ctap().info
        algs = [a.get("alg") for a in (i.algorithms or [])] or None
        return {"options": dict(i.options or {}), "algorithms": algs}

    def _uv(self, ctap: Any, cdh: bytes, perm: Any) -> dict[str, Any]:
        from fido2.ctap2.pin import ClientPin

        if (ctap.info.options or {}).get("uv"):
            return {"options": {"uv": True}}
        pin = self._pin() if self._pin else None
        if not pin:
            raise ClientError("remote_client.cancelled")
        cp = ClientPin(ctap)
        token = cp.get_pin_token(pin, perm, RP_ID)
        return {"pin_uv_param": cp.protocol.authenticate(token, cdh), "pin_uv_protocol": cp.protocol.VERSION}

    def make_credential(self, client_data_hash: bytes, user_name: str, prompt: Prompt) -> dict[str, Any]:
        from fido2.ctap2.pin import ClientPin

        ctap = self._ctap()
        extra = self._uv(ctap, client_data_hash, ClientPin.PERMISSION.MAKE_CREDENTIAL)
        opts = {"rk": False, **extra.pop("options", {})}
        r = ctap.make_credential(
            client_data_hash,
            {"id": RP_ID, "name": "orch remote"},
            {"id": os.urandom(16), "name": user_name, "displayName": user_name},
            [{"type": "public-key", "alg": ES256}],
            options=opts,
            **extra,
        )
        cd = r.auth_data.credential_data
        pk = cd.public_key
        return {
            "cred_id": bytes(cd.credential_id), "x": bytes(pk[-2]), "y": bytes(pk[-3]),
            "alg": pk.get(3), "flags": int(r.auth_data.flags),
        }  # fmt: skip

    def get_assertion(self, client_data_hash: bytes, cred_id: bytes, prompt: Prompt) -> dict[str, Any]:
        from fido2.ctap2.pin import ClientPin

        ctap = self._ctap()
        extra = self._uv(ctap, client_data_hash, ClientPin.PERMISSION.GET_ASSERTION)
        opts = extra.pop("options", None)
        r = ctap.get_assertion(RP_ID, client_data_hash, [{"type": "public-key", "id": cred_id}], options=opts, **extra)
        return {"auth_data": bytes(r.auth_data), "sig": bytes(r.signature)}
