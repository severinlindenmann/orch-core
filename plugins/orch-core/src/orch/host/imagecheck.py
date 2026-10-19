"""The host's check of a pasted screenshot (R3 part B): magic bytes, size, and the chunk headers. **No decoder.**

The browser decodes the picture it was given and encodes it again as PNG (or, over 5 MiB, scaled or WebP), which drops
EXIF/GPS, colour-profile text and any polyglot tail. This module is the second line, for a file that did not come from
that browser code: it never decodes pixels (no inflate, no image library), it only reads headers.

* PNG: the 8 byte signature, then chunk headers (length, type) walked from the first byte to ``IEND``. A chunk type
  that can carry text or a location (``eXIf``, ``tEXt``, ``zTXt``, ``iTXt``, ``iCCP``, ``tIME``, ``dSIG``) is refused,
  so is any byte after ``IEND``, a chunk that runs past the end, and a first chunk that is not ``IHDR``.
* WebP: ``RIFF`` + size + ``WEBP`` then chunk headers; only ``VP8 ``, ``VP8L``, ``VP8X`` (no metadata flag bits),
  ``ALPH`` and ``ANIM``-less stills are allowed; ``EXIF``, ``XMP `` and ``ICCP`` chunks and trailing bytes are refused.
* Everything else is refused: SVG (script), JPEG (EXIF), GIF, HEIC and unknown bytes. The type is read from the bytes,
  never from a name or a declared type.

CRC values are not checked: a damaged picture is only a picture that does not show, and that is the browser's
business, not a security property.
"""

from __future__ import annotations

import struct

__all__ = ["IMAGE_MAX", "ImageRefused", "check", "media_type"]

IMAGE_MAX = 5 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
#: chunks that can hold text, a place, a time or a profile: not allowed in a pasted picture
_PNG_FORBIDDEN = frozenset({b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"iCCP", b"tIME", b"dSIG"})
_PNG_CHUNKS_MAX = 4096
_WEBP_ALLOWED = frozenset({b"VP8 ", b"VP8L", b"VP8X", b"ALPH"})
_WEBP_METADATA_FLAGS = 0x20 | 0x08 | 0x04  # ICC profile, EXIF, XMP bits of the VP8X flags byte


class ImageRefused(ValueError):
    """The bytes are not a screenshot this host stores; ``str(exc)`` is a plain sentence."""


def media_type(data: bytes) -> str | None:
    """``image/png`` or ``image/webp`` by magic bytes, else ``None``."""
    if data.startswith(_PNG):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def check(data: bytes) -> str:
    """The media type of an acceptable picture, or :class:`ImageRefused`."""
    if not isinstance(data, bytes | bytearray):
        raise ImageRefused("the picture is not bytes")
    data = bytes(data)
    if not data:
        raise ImageRefused("the picture is empty")
    if len(data) > IMAGE_MAX:
        raise ImageRefused("the picture is larger than 5 MiB")
    kind = media_type(data)
    if kind == "image/png":
        _png(data)
    elif kind == "image/webp":
        _webp(data)
    else:
        head = data[:512].lstrip().lower()
        if head.startswith((b"<svg", b"<?xml", b"<!doctype svg")) or b"<svg" in head:
            raise ImageRefused("SVG pictures are not accepted (they can run scripts); use a PNG screenshot")
        if data.startswith(b"\xff\xd8\xff"):
            raise ImageRefused("JPEG pictures are not accepted here (they carry location data); paste a screenshot")
        raise ImageRefused("only PNG screenshots are accepted")
    return kind


def _png(data: bytes) -> None:
    pos, n, count = len(_PNG), len(data), 0
    first = True
    while True:
        if pos + 8 > n:
            raise ImageRefused("the PNG ends without its last chunk")
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        ctype = data[pos + 4 : pos + 8]
        count += 1
        if count > _PNG_CHUNKS_MAX:
            raise ImageRefused("the PNG has too many chunks")
        if not all(65 <= b <= 90 or 97 <= b <= 122 for b in ctype):
            raise ImageRefused("the PNG has a chunk with an invalid name")
        if first and ctype != b"IHDR":
            raise ImageRefused("the PNG does not start with its header chunk")
        first = False
        if ctype in _PNG_FORBIDDEN:
            raise ImageRefused("the PNG carries metadata (text, a profile or a location); take a plain screenshot")
        end = pos + 8 + length + 4  # header, data, CRC
        if end > n:
            raise ImageRefused("a PNG chunk runs past the end of the file")
        pos = end
        if ctype == b"IEND":
            if length != 0:
                raise ImageRefused("the PNG end chunk is not empty")
            if pos != n:
                raise ImageRefused("the PNG has bytes after its end")
            return


def _webp(data: bytes) -> None:
    n = len(data)
    (riff,) = struct.unpack("<I", data[4:8])
    if riff + 8 != n:
        raise ImageRefused("the WebP size does not match the file (bytes after its end?)")
    pos, count, seen_image = 12, 0, False
    while pos < n:
        if pos + 8 > n:
            raise ImageRefused("a WebP chunk is cut off")
        ctype = data[pos : pos + 4]
        (length,) = struct.unpack("<I", data[pos + 4 : pos + 8])
        count += 1
        if count > 64:
            raise ImageRefused("the WebP has too many chunks")
        if ctype not in _WEBP_ALLOWED:
            raise ImageRefused("the WebP carries metadata or is animated; take a plain screenshot")
        body = pos + 8
        if ctype == b"VP8X":
            if length < 1 or body >= n or data[body] & _WEBP_METADATA_FLAGS:
                raise ImageRefused("the WebP says it carries metadata")
        if ctype in (b"VP8 ", b"VP8L"):
            seen_image = True
        pos = body + length + (length & 1)
        if pos > n:
            raise ImageRefused("a WebP chunk runs past the end of the file")
    if not seen_image:
        raise ImageRefused("the WebP has no picture")
