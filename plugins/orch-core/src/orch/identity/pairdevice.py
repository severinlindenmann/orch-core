"""The pure half of the person-path material kind ``relay.pair_device`` (design 11A.15.10 item 12, 11A.15.6 step 9).

At step 9 R6 holds a verified certificate for the phone's keys and asks the person to sign ``device.added {device,
cert}`` with the Mac's F1 device. What the person reviews, and what the signer binds, is:

* the workspace id, the offer id and the attempt (host attestations: which pairing this is);
* the device id (``d_`` + the certificate's ``device_id``);
* the label: the cleaned label **exactly as** :func:`orch.identity.pairing.clean_label` returned it, shown for review
  only ("named by the device"): ``device.added`` carries no label and no NFC is applied (11A.15.13);
* the scopes the offer grants (the relay list scopes, a subset of ``look, decide``: no ``operate`` or ``type`` for a
  phone in v1.7, N1), within the certificate's ``scopes_max``;
* the certificate digest ``sha256:hex(sha256(cj(cert)))`` of the very ``cert`` the event embeds;
* the comparison code: the SAS on path 7a (a presented certificate), ``cert_code`` on path 7b (a minted one).

:class:`PairDeviceFacts` is what R6 hands the host (``orch.relay`` may import this module: it is pure, holds no key and
reads no clock). :func:`material` and :func:`device_added` are what ``human.prepare`` builds from it;
:func:`check_material` is the signer's recomputation (``orch.host.signer.check``), which refuses any extra or missing
field, a material of another kind, a convenience ``auth`` and every value it can check against the event. Each refusal
is :class:`PairDeviceInvalid` with a ``reason`` naming the binding that failed.
"""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping
from typing import Any

from orch import canon

from .pairing import clean_label

__all__ = [
    "COMPARISONS",
    "MATERIAL_FIELDS",
    "MATERIAL_KIND",
    "OP",
    "PHONE_SCOPES",
    "PairDeviceFacts",
    "PairDeviceInvalid",
    "cert_digest",
    "check_material",
    "device_added",
    "material",
]

#: the person op and the material kind (one name: the op emits only this kind and the kind belongs only to this op)
OP = "relay.pair_device"
MATERIAL_KIND = "relay.pair_device"
#: the scopes a phone may hold in v1.7 (N1: no ``operate``, never ``type``), in their canonical order
PHONE_SCOPES = ("look", "decide")
#: path -> the code the person compared: 7a (a presented certificate) the SAS, 7b (a minted one) ``cert_code``
COMPARISONS = {"7a": "sas", "7b": "cert_code"}
#: the exact field set of the material (``review`` is added by the host and checked by the signer's review rule)
MATERIAL_FIELDS = frozenset(
    {"kind", "workspace_id", "offer_id", "attempt", "device", "label", "scopes", "cert_digest", "comparison"}
)
_EVENT_PAYLOAD = frozenset({"device", "cert"})
_ENVELOPE = frozenset({"v", "id", "type", "actor", "auth", "hash_v", "roster_v", "based_on", "sig"})
_HEX32 = re.compile(r"[0-9a-f]{32}")
_DEVICE = re.compile(r"d_[0-9a-f]{32}")
_CODE = re.compile(r"[A-Z2-7]{6}")  # RFC 4648 base32, 6 characters (sas, cert_code: protocol §4)


class PairDeviceInvalid(ValueError):
    """The facts or the material are outside the contract; ``reason`` names the binding."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def _hex32(v: object) -> bool:
    return type(v) is str and _HEX32.fullmatch(v) is not None


def _who_ok(who: Any) -> bool:
    """Exactly one of ``{"person_id": 32 hex}`` and ``{"pk_pin": b64u of 32 bytes}`` (protocol §8.3, reader 9.3)."""
    if type(who) is not dict or len(who) != 1:
        return False
    if "person_id" in who:
        return _hex32(who["person_id"])
    if "pk_pin" in who and type(who["pk_pin"]) is str:
        from orch import crypto

        try:
            crypto.unb64u(who["pk_pin"], 32)
        except crypto.EncodingError:
            return False
        return True
    return False


def cert_digest(cert: Any) -> str:
    """``sha256:`` + hex of ``sha256(cj(cert))``: the digest the review shows of the signed certificate."""
    return "sha256:" + hashlib.sha256(canon.dumps(cert)).hexdigest()


def _cert_o(cert: Any) -> dict[str, Any]:
    """The certificate object, shape-checked for what the review and the binding read. The signature is not checked
    here: R6 verified it under the pinned owner key (step 8) and the model verifies it again at the append."""
    o = cert.get("o") if type(cert) is dict and set(cert) == {"o", "sig"} else None
    if type(o) is not dict or not _hex32(o.get("device_id")) or not _hex32(o.get("person_id")):
        raise PairDeviceInvalid("cert", "not a device certificate {o, sig} naming a device and a person")
    scopes = o.get("scopes_max")
    if type(scopes) is not list or not all(type(s) is str for s in scopes) or len(set(scopes)) != len(scopes):
        raise PairDeviceInvalid("cert", "scopes_max is not a list of distinct scopes")
    if not set(scopes) <= set(PHONE_SCOPES) or "decide" not in scopes:
        raise PairDeviceInvalid("cert", "a phone certificate holds decide and at most look (no operate, no type)")
    return o


def _scopes(scopes: Any, ceiling: list[str]) -> list[str]:
    if (
        type(scopes) not in (list, tuple)
        or not scopes
        or not all(type(s) is str for s in scopes)
        or list(scopes) != [s for s in PHONE_SCOPES if s in scopes]
    ):
        raise PairDeviceInvalid("scopes", "a non-empty subset of look, decide in that order")
    if not set(scopes) <= set(ceiling):
        raise PairDeviceInvalid("scopes", "the offer's scopes exceed the certificate's scopes_max")
    return list(scopes)


def _label(label: Any) -> str:
    if type(label) is not str:
        raise PairDeviceInvalid("label", "not a string")
    try:
        cleaned = clean_label(label)
    except Exception:  # noqa: BLE001 - clean_label refuses lone surrogates
        raise PairDeviceInvalid("label", "not a label") from None
    if cleaned != label:
        raise PairDeviceInvalid("label", "not the output of clean_label (protocol §2.5)")
    return label


class PairDeviceFacts:
    """What R6 hands the host at step 9, all already verified by R6 (11A.15.6 steps 3 to 8). Validated on
    construction and immutable: the certificate and the declaration are kept as canonical bytes and every read
    returns a fresh copy.

    * ``workspace_id``, ``offer_id``, ``attempt``: 32 lower-case hex;
    * ``cert``: the verified device certificate ``{o, sig}`` of the phone's keys, of the owner's person;
    * ``label``: the cleaned label (``clean_label``'s output, byte for byte);
    * ``scopes``: the offer's scopes, a subset of ``look, decide`` in that order, within ``cert.o.scopes_max``;
    * ``path``: ``"7a"`` (a presented certificate) or ``"7b"`` (a minted one); ``code``: the SAS (7a) or
      ``cert_code`` (7b) the person compared, 6 base32 characters (kept out of ``repr``);
    * ``decl``: the phone's stored reader declaration (``bind.via:"pair"``, the first one stored, step 4) or ``None``;
      ``decl_who``: the ``who`` it was verified against at step 4 (``{"pk_pin"}`` from the link, or
      ``{"person_id"}`` of a presented certificate), given exactly when ``decl`` is."""

    __slots__ = ("workspace_id", "offer_id", "attempt", "label", "scopes", "path", "code", "_cert", "_decl", "_who")

    def __init__(
        self,
        *,
        workspace_id: str,
        offer_id: str,
        attempt: str,
        cert: dict[str, Any],
        label: str,
        scopes: tuple[str, ...] | list[str],
        path: str,
        code: str,
        decl: dict[str, Any] | None = None,
        decl_who: dict[str, str] | None = None,
    ) -> None:
        for name, value in (("workspace_id", workspace_id), ("offer_id", offer_id), ("attempt", attempt)):
            if not _hex32(value):
                raise PairDeviceInvalid(name, "not 32 lower-case hex")
        o = _cert_o(cert)
        _label(label)
        checked = tuple(_scopes(scopes, o["scopes_max"]))
        if path not in COMPARISONS:
            raise PairDeviceInvalid("path", "7a or 7b")
        if type(code) is not str or not _CODE.fullmatch(code):
            raise PairDeviceInvalid("code", "6 base32 characters")
        if (decl is None) != (decl_who is None):
            raise PairDeviceInvalid("decl", "a declaration and the who it was verified for come together")
        if decl_who is not None and not _who_ok(decl_who):
            raise PairDeviceInvalid("decl", "decl_who is exactly {person_id: 32 hex} or {pk_pin: b64u of 32 bytes}")
        try:
            raw_cert = canon.dumps(cert)
            raw_decl = None if decl is None else canon.dumps(decl)
        except Exception:  # noqa: BLE001 - not canonical JSON
            raise PairDeviceInvalid("cert", "not canonical JSON") from None
        for name, value in (
            ("workspace_id", workspace_id),
            ("offer_id", offer_id),
            ("attempt", attempt),
            ("label", label),
            ("scopes", checked),
            ("path", path),
            ("code", code),
            ("_cert", raw_cert),
            ("_decl", raw_decl),
            ("_who", None if decl_who is None else dict(decl_who)),
        ):
            object.__setattr__(self, name, value)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("PairDeviceFacts is immutable")

    def __delattr__(self, name: str) -> None:
        raise AttributeError("PairDeviceFacts is immutable")

    def __repr__(self) -> str:  # never the code or the declaration: they stay out of logs and tracebacks
        return (
            f"PairDeviceFacts(workspace_id={self.workspace_id!r}, offer_id={self.offer_id!r}, "
            f"attempt={self.attempt!r}, device={self.device!r}, scopes={self.scopes!r}, path={self.path!r})"
        )

    def _key(self) -> tuple[Any, ...]:
        who = None if self._who is None else tuple(sorted(self._who.items()))
        return (self.workspace_id, self.offer_id, self.attempt, self.label, self.scopes, self.path, self.code,
                self._cert, self._decl, who)  # fmt: skip

    def __eq__(self, other: object) -> bool:
        return self._key() == other._key() if isinstance(other, PairDeviceFacts) else NotImplemented

    def __hash__(self) -> int:
        return hash(self._key())

    def __copy__(self) -> PairDeviceFacts:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> PairDeviceFacts:
        return self  # immutable: the copies happen on every read

    @property
    def cert(self) -> dict[str, Any]:
        return canon.loads_strict(self._cert)

    @property
    def decl(self) -> dict[str, Any] | None:
        return None if self._decl is None else canon.loads_strict(self._decl)

    @property
    def decl_who(self) -> dict[str, str] | None:
        return None if self._who is None else dict(self._who)

    @property
    def device(self) -> str:
        return "d_" + self.cert["o"]["device_id"]

    @property
    def person(self) -> str:
        return "p_" + self.cert["o"]["person_id"]

    @property
    def comparison(self) -> dict[str, str]:
        return {"kind": COMPARISONS[self.path], "code": self.code}


def material(facts: PairDeviceFacts) -> dict[str, Any]:
    """The ``relay.pair_device`` material, without its ``review`` (the host adds it, as for every kind)."""
    return {
        "kind": MATERIAL_KIND,
        "workspace_id": facts.workspace_id,
        "offer_id": facts.offer_id,
        "attempt": facts.attempt,
        "device": facts.device,
        "label": facts.label,
        "scopes": list(facts.scopes),
        "cert_digest": cert_digest(facts.cert),
        "comparison": facts.comparison,
    }


def device_added(facts: PairDeviceFacts) -> dict[str, Any]:
    """The event's type and payload: exactly ``device.added {device, cert}`` (F1 §5.3); no label."""
    return {"type": "device.added", "device": facts.device, "cert": facts.cert}


def check_material(
    material: Mapping[str, Any], event: Mapping[str, Any], *, workspace_id: str, person: str
) -> dict[str, Any]:
    """The signer's binding of a ``relay.pair_device`` review (before any prompt): ``material`` has exactly
    :data:`MATERIAL_FIELDS` and ``review``, its kind is this one, the workspace is the signer's, the event is exactly
    ``device.added {device, cert}`` and not convenience-signed, the certificate is a phone certificate of ``person``
    (the signer's), the device id, the certificate digest, the label (``clean_label``'s fixed point), the scopes (within
    the certificate) and the comparison code are what they must be. Returns the bound fields; raises
    :class:`PairDeviceInvalid`."""
    if not isinstance(material, Mapping) or set(material) != MATERIAL_FIELDS | {"review"}:
        raise PairDeviceInvalid("material", "fields outside or missing from the relay.pair_device material")
    if material["kind"] != MATERIAL_KIND:
        raise PairDeviceInvalid("kind", f"material of kind {material['kind']!r}")
    if not _hex32(workspace_id) or material["workspace_id"] != workspace_id:
        raise PairDeviceInvalid("workspace_id", "the material is for another workspace")
    for name in ("offer_id", "attempt"):
        if not _hex32(material[name]):
            raise PairDeviceInvalid(name, "not 32 lower-case hex")
    if not isinstance(event, Mapping) or event.get("type") != "device.added":
        raise PairDeviceInvalid("event", "the event is not device.added")
    if set(event) - _ENVELOPE != _EVENT_PAYLOAD:
        raise PairDeviceInvalid("event", "device.added carries exactly device and cert")
    if event.get("auth") == "convenience":
        raise PairDeviceInvalid("auth", "relay.pair_device is never signed with convenience custody")
    o = _cert_o(event["cert"])
    if "p_" + o["person_id"] != person:
        raise PairDeviceInvalid("cert", "the certificate is not of the signer's person")
    device = "d_" + o["device_id"]
    if not (type(material["device"]) is str and _DEVICE.fullmatch(material["device"])):
        raise PairDeviceInvalid("device", "malformed")
    if not material["device"] == event.get("device") == device:
        raise PairDeviceInvalid("device", "the device id, the event's device and the certificate's differ")
    if material["cert_digest"] != cert_digest(event["cert"]):
        raise PairDeviceInvalid("cert_digest", "not the digest of the certificate the event embeds")
    _label(material["label"])
    if type(material["scopes"]) is not list:
        raise PairDeviceInvalid("scopes", "not a list")
    _scopes(material["scopes"], o["scopes_max"])
    comparison = material["comparison"]
    if (
        type(comparison) is not dict
        or set(comparison) != {"kind", "code"}
        or comparison["kind"] not in COMPARISONS.values()
        or type(comparison["code"]) is not str
        or not _CODE.fullmatch(comparison["code"])
    ):
        raise PairDeviceInvalid("comparison", "{kind: sas | cert_code, code: 6 base32 characters}")
    return {k: copy.deepcopy(material[k]) for k in MATERIAL_FIELDS}
