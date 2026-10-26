"""The v2 ws->ws envelope as the router sees it (``contracts/relay/protocol-v2.md`` §11.1, §11.3).

``envelope = header (60) || u32(body_len) || body || signature (64) [|| u32(cosig_len) || cj(cosig)]``
``header   = "ORWX" || u8(2) || u8(suite) || u8(flags) || u8(0) || id (16) || from_ws (16) || to_ws (16) || u32(wxk_version)``

The router reads the header and checks the sender's ``WSK`` signature over ``label || header || body``; it never opens
the body (sealed to the receiver's ``WXK``) and never changes a byte: what is forwarded is the sender's bytes.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..contract import suite as S
from ..contract.hub import Refused

MAGIC = b"ORWX"
HEADER_LEN = 60
MAX_ENVELOPE = 1 << 20  # protocol-v2 §11.1: larger attachments go through Drop
SIG_LABEL = b"orch/v2/ws-envelope|"
FLAG_COSIGNED, FLAG_REFUSAL = 0x01, 0x02


@dataclass(frozen=True)
class Header:
    envelope_id: str  # hex
    from_ws: str  # hex
    to_ws: str  # hex
    flags: int
    wxk_version: int


def parse(raw: bytes) -> tuple[Header, bytes, bytes]:
    """Structure check only. Returns ``(header, signed_bytes, signature)``; raises ``Refused``: ``too_large`` or
    ``malformed``. The signature is checked by :func:`verify`."""
    if len(raw) > MAX_ENVELOPE:
        raise Refused("too_large")
    if len(raw) < HEADER_LEN + 4 + 64 or raw[:4] != MAGIC or raw[4] != 2 or raw[5] != S.SUITE_ID or raw[7] != 0:
        raise Refused("malformed")
    flags = raw[6]
    if flags & ~(FLAG_COSIGNED | FLAG_REFUSAL):
        raise Refused("malformed")
    (body_len,) = struct.unpack(">I", raw[HEADER_LEN : HEADER_LEN + 4])
    sig_at = HEADER_LEN + 4 + body_len
    if sig_at + 64 > len(raw):
        raise Refused("malformed")
    rest = raw[sig_at + 64 :]
    if flags & FLAG_COSIGNED:
        if len(rest) < 4 or struct.unpack(">I", rest[:4])[0] != len(rest) - 4 or len(rest) == 4:
            raise Refused("malformed")
    elif rest:
        raise Refused("malformed")
    h = Header(raw[8:24].hex(), raw[24:40].hex(), raw[40:56].hex(), flags, struct.unpack(">I", raw[56:60])[0])
    return h, raw[:sig_at], raw[sig_at : sig_at + 64]


def verify(signed: bytes, sig: bytes, wsk_pub: bytes) -> None:
    """The sender's WSK over ``label || header || body``. ``signed`` is what :func:`parse` returned (header, the length
    field, body); the signature does not cover the length field, so it is cut out here."""
    header, body = signed[:HEADER_LEN], signed[HEADER_LEN + 4 :]
    if not S.verify(wsk_pub, sig, SIG_LABEL + header + body):
        raise Refused("bad_signature")
