"""Pure builders for R6 pairing (design 11A.15.10, H12c items 1-4): the epoch-key commitment, the two 6-character codes,
the pairing links and the pair request's meta (the six answers of item 5 are ``orch.identity.answers``).

Specs: ``contracts/relay/protocol-v2.md`` §2.5 (labels), §4 (pins, SAS, ``cert_code``), §7.2 (``wk_commit``), §8.3
(links, the pair meta), §8.4 (the challenge). Vectors: the relay corpus sections ``wk_commit``, ``bridge.sas``,
``cert_request.cert_code``, ``bridge.links`` and the epoch-0 ``bridge.host_cases`` (``tests/identity/test_pairing.py``).

No I/O, no clock, no key held. Every function validates its inputs exactly and raises :class:`Refused` with code
``malformed`` (the protocol's code for a bad meta shape) on anything outside the contract; the message never carries a
secret.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

from orch import canon, crypto
from orch.canon.text import is_assigned, is_format
from orch.crypto.labels import L
from orch.wsproto.wire import WireError, check_origin

from .errors import Refused

__all__ = [
    "CHALLENGE_FIELDS",
    "MAX_EPOCH",
    "MAX_LABEL",
    "MAX_LINK",
    "PAIR_META_FIELDS",
    "READER_DECL_MAX",
    "PairFields",
    "PairLink",
    "PairMeta",
    "cert_code",
    "check_pair_meta",
    "clean_label",
    "pair_link",
    "parse_pair_fragment",
    "parse_pair_link",
    "render_pair_link",
    "sas",
    "wk_commit",
]

MAX_EPOCH = 2**32 - 1  # protocol §7.2: an epoch is a u32 in 1 .. 2^32-1 wherever it is parsed


def _bad(what: str) -> Refused:
    return Refused("malformed", what)


def _id(value: object, what: str) -> bytes:
    """A 16-byte id in its one text form, 32 lower-case hex characters (protocol §2.2)."""
    try:
        return crypto.unhex(value, 16)
    except crypto.EncodingError:
        raise _bad(what) from None


def _raw(value: object, n: int, what: str) -> bytes:
    if type(value) is not bytes or len(value) != n:
        raise _bad(what)
    return value


def _pub(value: object, what: str) -> bytes:
    """A suite-2 public key: 65 raw bytes, uncompressed, on the curve (``dk_sig``, ``dk_kx``, ``WSK``, ``PK``)."""
    if type(value) is not bytes:
        raise _bad(what)
    try:
        return crypto.validate_public_key(value)
    except crypto.CryptoError:
        raise _bad(what) from None


def _b64_pub(value: object, what: str) -> bytes:
    try:
        raw = crypto.unb64u(value, crypto.PUB_LEN)
    except crypto.EncodingError:
        raise _bad(what) from None
    return _pub(raw, what)


_SIG_LABEL = {
    "member_list": "sig_member_list",
    "wk_grant": "sig_wk_grant",
    "cert_request": "sig_cert_request",
    "cert_challenge": "sig_cert_challenge",
    "cert_rejection": "sig_cert_rejection",
}


def _verify_signed(pub: bytes, signed: object, kind: str) -> dict | None:
    """The one signed-object verifier of the H12c builders: ``o`` if ``signed`` is exactly ``{o, sig}`` with ``v: 2``,
    this suite and ``kind`` (integers, not bools), and ``sig`` verifies under ``pub`` over ``label(kind) || cj(o)``
    (proto §2.4); ``None`` otherwise. The exact field set is the caller's check."""
    try:
        if type(signed) is not dict or set(signed) != {"o", "sig"}:
            return None
        o = signed["o"]
        if type(o) is not dict or type(o.get("v")) is not int or type(o.get("suite")) is not int:
            return None
        if o["v"] != 2 or o["suite"] != crypto.SUITE_ID or o.get("kind") != kind:
            return None
        sig = crypto.unb64u(signed["sig"], crypto.SIG_LEN)
        return o if crypto.verify(pub, sig, L[_SIG_LABEL[kind]] + canon.dumps(o)) else None
    except (crypto.EncodingError, crypto.CryptoError, canon.JcsError, ValueError, KeyError, TypeError):
        return None


def _lp16(b: bytes) -> bytes:
    return len(b).to_bytes(2, "big") + b


_CODE_SHAPE = re.compile(r"[A-Z2-7]{6}")


def normalize_code(text: object) -> str | None:
    """A typed pairing code (the SAS or ``cert_code``) as the host compares it: stripped, upper-cased, exactly six
    base32 characters (``A-Z2-7``); anything else is ``None`` (B1b: the typed text is then discarded, never sent)."""
    if type(text) is not str:
        return None
    t = text.strip().upper()
    return t if _CODE_SHAPE.fullmatch(t) else None


def _code(digest: bytes) -> str:
    """The first 6 characters of RFC 4648 base32 (``A-Z2-7``, 30 bits) of a 32-byte digest."""
    return base64.b32encode(digest).decode("ascii")[:6]


# --- 1. wk_commit (§7.2, G2b) ----------------------------------------------------------------------------------------


def wk_commit(workspace_id: str, epoch: int, wk: bytes) -> str:
    """``b64u(H("orch/v2/wk-commit|" || workspace_id || u32(epoch) || WK_epoch))``: the key commitment every member list
    at ``epoch`` carries. No suite byte (unlike the pins), as the protocol and the vectors have it."""
    ws = _id(workspace_id, "workspace_id")
    if type(epoch) is not int or not 1 <= epoch <= MAX_EPOCH:
        raise _bad("epoch")
    key = _raw(wk, 32, "wk")
    return crypto.b64u(crypto.sha256(L["h_wk_commit"] + ws + epoch.to_bytes(4, "big") + key))


# --- 2. the two codes (§4, §8.4) -------------------------------------------------------------------------------------


def sas(workspace_id: str, offer_id: str, dk_sig_pub: bytes, dk_kx_pub: bytes, wsk_pub: bytes, sas_nonce: bytes) -> str:
    """The 6-character pairing code: ``base32(H("orch/v2/sas|" || suite || ws || offer || lp16(dk_sig_pub) ||
    lp16(dk_kx_pub) || lp16(wsk_pub) || sas_nonce))[0:6]``. Ids are 32 lower-case hex; keys are raw 65-byte points (the
    wire's b64u decoded); ``sas_nonce`` is the offer's 32 raw bytes. The host shows it for the request it consumed;
    the device for the signed ``pending`` answer."""
    ws, offer = _id(workspace_id, "workspace_id"), _id(offer_id, "offer_id")
    keys = [_pub(k, w) for k, w in ((dk_sig_pub, "dk_sig_pub"), (dk_kx_pub, "dk_kx_pub"), (wsk_pub, "wsk_pub"))]
    nonce = _raw(sas_nonce, 32, "sas_nonce")
    pre = L["h_sas"] + bytes([crypto.SUITE_ID]) + ws + offer + b"".join(_lp16(k) for k in keys) + nonce
    return _code(crypto.sha256(pre))


CHALLENGE_FIELDS = frozenset(
    {
        "v",
        "suite",
        "kind",
        "request_id",
        "workspace_id",
        "person_id",
        "offer_id",
        "attempt",
        "device_id",
        "dk_sig_pub",
        "dk_kx_pub",
        "primary_nonce",
        "expires_ms",
    }
)
_CHALLENGE_IDS = ("request_id", "workspace_id", "person_id", "offer_id", "attempt", "device_id")
_MAX_SAFE_INT = 2**53 - 1


def cert_code(challenge_o: dict) -> str:
    """The 6-character code of the primary's challenge: ``base32(H("orch/v2/sas-cert|" || suite || cj(o)))[0:6]``, over
    the challenge **object** ``o`` (not the ``{o, sig}`` wrapper). The object must have the exact §8.4 (G3b) field set
    and types: an old-shape challenge (no ``offer_id``/``attempt``) has no code. This is the code only; the
    signature, the pins and the binding to the attempt are the caller's checks (§8.4 step 5)."""
    o = challenge_o
    if type(o) is not dict or set(o) != CHALLENGE_FIELDS:
        raise _bad("challenge field set")
    if o["v"] != 2 or type(o["v"]) is not int or o["suite"] != crypto.SUITE_ID or type(o["suite"]) is not int:
        raise _bad("challenge version or suite")
    if o["kind"] != "cert_challenge":
        raise _bad("challenge kind")
    for k in _CHALLENGE_IDS:
        _id(o[k], k)
    _b64_pub(o["dk_sig_pub"], "dk_sig_pub")
    _b64_pub(o["dk_kx_pub"], "dk_kx_pub")
    try:
        crypto.unb64u(o["primary_nonce"], 32)
    except crypto.EncodingError:
        raise _bad("primary_nonce") from None
    if type(o["expires_ms"]) is not int or not 0 <= o["expires_ms"] <= _MAX_SAFE_INT:
        raise _bad("expires_ms")
    return _code(crypto.sha256(L["h_sas_cert"] + bytes([crypto.SUITE_ID]) + canon.dumps(o)))


# --- 3. pair links (§8.3 step 1, D45/D46) ----------------------------------------------------------------------------
#
# Two forms carry the same five fields:
#
#   https://<origin>/pair#v2.<ws hex>.<offer hex>.<b64u S>.<b64u wsk_pin>.<b64u pk_pin>
#   orch://pair?relay=<percent-encoded origin>#v2.<the same five fields>
#
# **The QR carries the native ``orch://`` link** (design 11A.15.12 G2, Q5): a browser page served by the relay would
# see an HTTPS link's fragment, the app's URL handler does not. The HTTPS form is the fallback a person can type or
# open. Both are printed on the person's terminal only (11A.15.9 exposure rules).
#
# Canonical spellings: the origin as 11A.2 stores it (``orch.wsproto.wire.check_origin``: lower case, no path, no
# trailing slash, ``http`` only for loopback); the path exactly ``/pair`` (``/app/pair`` is an alias the app may
# accept, this parser does not); in the native form, ``relay`` is the only query parameter and its value escapes every
# byte outside RFC 3986 unreserved (``A-Z a-z 0-9 - . _ ~``) as upper-case ``%XX`` (``https%3A%2F%2F...``, what
# JavaScript's ``encodeURIComponent`` gives for an origin). :func:`parse_pair_link` accepts a text only if rendering
# its parsed fields gives the same text back.

MAX_LINK = 2048  # bytes; a link longer than any canonical one is refused before parsing (orch-mobile's bound)
_FRAGMENT = re.compile(
    r"v2\.([0-9a-f]{32})\.([0-9a-f]{32})\.([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{43})\.([A-Za-z0-9_-]{43})"
)
_UNRESERVED = frozenset(b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_NATIVE_HEAD = "orch://pair?relay="
_HTTPS_PATH = "/pair"


@dataclass(frozen=True, slots=True)
class PairFields:
    """The five fields after ``v2``. ``secret`` (``S``) never appears in a repr."""

    workspace_id: str
    offer_id: str
    secret: bytes = field(repr=False)
    wsk_pin: bytes
    pk_pin: bytes

    def __post_init__(self) -> None:
        """The invariants hold at construction, so :func:`render_pair_link` never renders a link that
        :func:`parse_pair_link` refuses."""
        _id(self.workspace_id, "workspace_id")
        _id(self.offer_id, "offer_id")
        _raw(self.secret, 32, "secret")
        _raw(self.wsk_pin, 32, "wsk_pin")
        _raw(self.pk_pin, 32, "pk_pin")

    def fragment(self) -> str:
        return ".".join(
            (
                "v2",
                self.workspace_id,
                self.offer_id,
                *(crypto.b64u(b) for b in (self.secret, self.wsk_pin, self.pk_pin)),
            )
        )


@dataclass(frozen=True, slots=True)
class PairLink:
    """A parsed whole link: the relay origin it names, its form, and the fields."""

    origin: str
    native: bool
    fields: PairFields

    def __post_init__(self) -> None:
        _origin(self.origin)
        if type(self.native) is not bool:
            raise _bad("native")
        if type(self.fields) is not PairFields:
            raise _bad("fields")


def _origin(origin: object) -> str:
    try:
        return check_origin(origin)
    except WireError:
        raise _bad("origin") from None


def _pct(text: str) -> str:
    return "".join(chr(b) if b in _UNRESERVED else f"%{b:02X}" for b in text.encode("ascii"))


def render_pair_link(link: PairLink) -> str:
    """The one canonical spelling of ``link`` in its form (a :class:`PairLink` is valid by construction)."""
    if type(link) is not PairLink:
        raise _bad("link")
    origin = link.origin
    frag = link.fields.fragment()
    return f"{_NATIVE_HEAD}{_pct(origin)}#{frag}" if link.native else f"{origin}{_HTTPS_PATH}#{frag}"


def pair_link(
    origin: str, workspace_id: str, offer_id: str, secret: bytes, wsk_pub: bytes, pk_pub: bytes
) -> tuple[str, str]:
    """``(https, native)`` links for an offer. The pins are derived here from the keys (``wsk_pin`` of the own WSK,
    ``pk_pin`` of the pinned owner ``PK``), so a caller cannot pass a pin of the wrong key kind. The QR carries
    ``native``."""
    fields = PairFields(
        workspace_id=crypto.hex_id(_id(workspace_id, "workspace_id")),
        offer_id=crypto.hex_id(_id(offer_id, "offer_id")),
        secret=_raw(secret, 32, "secret"),
        wsk_pin=crypto.wsk_pin(_pub(wsk_pub, "wsk_pub")),
        pk_pin=crypto.pk_pin(_pub(pk_pub, "pk_pub")),
    )
    origin = _origin(origin)
    return (
        render_pair_link(PairLink(origin=origin, native=False, fields=fields)),
        render_pair_link(PairLink(origin=origin, native=True, fields=fields)),
    )


def parse_pair_fragment(fragment: str) -> PairFields:
    """The relay's fragment rule (``bridge.links``): an optional single leading ``#``, exactly ``v2``, lower-case hex
    ids, and canonical 43-character b64u of 32 bytes for each of the last three fields."""
    if type(fragment) is not str:
        raise _bad("fragment")
    m = _FRAGMENT.fullmatch(fragment.removeprefix("#"))
    if m is None:
        raise _bad("fragment")
    try:
        s, wp, pp = (crypto.unb64u(m.group(i), 32) for i in (3, 4, 5))
    except crypto.EncodingError:
        raise _bad("fragment") from None
    return PairFields(workspace_id=m.group(1), offer_id=m.group(2), secret=s, wsk_pin=wp, pk_pin=pp)


def _unpct(value: str) -> str:
    """Strict ``%XX`` decoding of the ``relay`` value; canonicality is checked by re-rendering, not here."""
    out = bytearray()
    i = 0
    while i < len(value):
        c = value[i]
        if c == "%":
            h = value[i + 1 : i + 3]
            if len(h) != 2 or not all(x in "0123456789ABCDEFabcdef" for x in h):
                raise _bad("relay")
            out.append(int(h, 16))
            i += 3
        else:
            out.append(ord(c))
            i += 1
    try:
        return out.decode("ascii")
    except UnicodeDecodeError:
        raise _bad("relay") from None


def parse_pair_link(text: str) -> PairLink:
    """Parse a whole pairing link, strictly: one of the two canonical forms above, printable ASCII, at most
    :data:`MAX_LINK` bytes, exactly one ``#``. Anything that does not re-render to the same text is refused."""
    if type(text) is not str or not text.isascii() or not 0 < len(text) <= MAX_LINK:
        raise _bad("link")
    if not all(0x21 <= ord(c) < 0x7F for c in text) or text.count("#") != 1:
        raise _bad("link")
    head, _, frag = text.partition("#")
    fields = parse_pair_fragment(frag)
    if head.startswith(_NATIVE_HEAD):
        link = PairLink(origin=_origin(_unpct(head[len(_NATIVE_HEAD) :])), native=True, fields=fields)
    elif head.endswith(_HTTPS_PATH):
        link = PairLink(origin=_origin(head[: -len(_HTTPS_PATH)]), native=False, fields=fields)
    else:
        raise _bad("link")
    if render_pair_link(link) != text:
        raise _bad("link not canonical")
    return link


# --- 4. the pair request's meta and its label (§8.3 steps 2-3, §2.5) -------------------------------------------------

MAX_LABEL = 80  # code points, protocol §2.5 (the relay reference's MAX_LABEL)
# Default_Ignorable_Code_Point (Unicode DerivedCoreProperties), the relay reference's table verbatim.
_IGNORABLE = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)


def _fixed_category(o: int) -> bool:
    """Cc, Zl, Zp and Co: fixed sets under the Unicode stability policy, so literal ranges are exact for every
    version."""
    return (
        o <= 0x1F
        or 0x7F <= o <= 0x9F
        or o in (0x2028, 0x2029)
        or 0xE000 <= o <= 0xF8FF
        or 0xF0000 <= o <= 0xFFFFD
        or 0x100000 <= o <= 0x10FFFD
    )


def _removed(c: str) -> bool:
    if c == "\n":
        return False
    o = ord(c)
    return (
        _fixed_category(o)
        or is_format(o)  # Cf, Unicode 16.0 (orch.canon._unicode16)
        or not is_assigned(o)  # Cn, Unicode 16.0
        or any(a <= o <= b for a, b in _IGNORABLE)
    )


def clean_label(text: str) -> str:
    """A device-chosen label as the receiving host records it (protocol §2.5, bridge v1 §9.3): every code point of
    category Cc (except LF), Cf, Zl, Zp, Co and Cn removed, every Default_Ignorable_Code_Point removed, **no
    normalisation**, then truncated to :data:`MAX_LABEL` code points. Lone surrogates are refused (strict JSON never
    yields them). ``Cn`` and ``Cf`` are Unicode **16.0**, from core's vendored tables (``orch.canon._unicode16``, the
    version ticket-format §11.3 pins), never the running Python's ``unicodedata``: a label is recorded the same on
    every interpreter and across an upgrade. A display shows the result as named by the device, never as an
    authenticated name.

    No NFC here (decided 15 Oct 2026: exactly §2.5, as the relay reference and the vectors have it). NFC is applied
    only when core writes an F1 copy of the label (the ticket format's own text rule), never to the recorded or
    displayed pairing label."""
    if type(text) is not str or any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise _bad("label")
    return "".join(c for c in text if not _removed(c))[:MAX_LABEL]


PAIR_META_FIELDS = frozenset({"op", "dk_sig_pub", "dk_kx_pub", "label", "cert", "attempt"})
READER_DECL_MAX = 2048  # bytes of cj(reader_decl) (§8.3 meta v2; the relay reference's READER_DECL_MAX)


def _reader_decl_shape(decl: object) -> dict:
    """The bridge's part of ``reader_decl`` (§8.3 step 3.1): a signed object ``{"o": {...}, "sig": any}`` whose ``cj``
    is at most :data:`READER_DECL_MAX` bytes. The type of ``sig``, the form of ``o``, the signature, the binding and
    the KAT are the host's H12d gate (``verify_decl``, H12d-3), never a ``malformed`` refusal here."""
    if type(decl) is not dict or set(decl) != {"o", "sig"} or type(decl["o"]) is not dict:
        raise _bad("reader_decl")
    try:
        size = len(canon.dumps(decl))
    except (canon.JcsError, ValueError, TypeError):
        raise _bad("reader_decl") from None
    if size > READER_DECL_MAX:
        raise _bad("reader_decl size")
    return decl


@dataclass(frozen=True, slots=True)
class PairMeta:
    """A pair request's meta that passed the §8.3 step 3.1 shape check. ``label`` is cleaned; ``device_id`` is derived
    from ``dk_sig_pub`` (the caller compares it with the header's, step 3.3); ``cert`` is passed on for step 3.5."""

    dk_sig_pub: bytes
    dk_kx_pub: bytes
    device_id: str
    label: str
    attempt: str
    cert: object
    reader_decl: dict | None = None


def check_pair_meta(meta: dict) -> PairMeta:
    """§8.3 step 3.1 (``malformed``): exactly the six fields ``op`` (= ``"pair"``), ``dk_sig_pub``, ``dk_kx_pub``,
    ``label``, ``cert``, ``attempt``, or those six and ``reader_decl`` (meta v2, H12d); both keys canonical b64u of
    valid suite-2 points; ``attempt`` 32 lower-case hex; ``label`` a string, returned cleaned. ``cert`` is ``null`` or
    any value: its checks are step 3.5's (``other_person``, ``cert_invalid``, ``cert_expired``, ``revoked``), as in the
    relay reference. ``reader_decl`` is shape-checked only and returned verbatim and **unverified** (``None`` when
    absent, which means level 0); the host's H12d gate verifies it at step 5."""
    if type(meta) is not dict or set(meta) - {"reader_decl"} != PAIR_META_FIELDS or meta["op"] != "pair":
        raise _bad("pair meta field set")
    decl = _reader_decl_shape(meta["reader_decl"]) if "reader_decl" in meta else None
    sig_pub = _b64_pub(meta["dk_sig_pub"], "dk_sig_pub")
    kx_pub = _b64_pub(meta["dk_kx_pub"], "dk_kx_pub")
    attempt = crypto.hex_id(_id(meta["attempt"], "attempt"))
    return PairMeta(
        dk_sig_pub=sig_pub,
        dk_kx_pub=kx_pub,
        device_id=crypto.hex_id(crypto.device_id(sig_pub)),
        label=clean_label(meta["label"]),
        attempt=attempt,
        cert=meta["cert"],
        reader_decl=decl,
    )
