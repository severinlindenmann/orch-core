"""W18 (#1090): a real macOS screenshot (colour profile, EXIF, text chunks) attached as evidence is shown as a picture,
not the fallback card; a pasted screenshot keeps the strict check."""

from __future__ import annotations

import struct
import zlib

from orch_dashboard import reads


def chunk(kind: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(*extra: bytes) -> bytes:
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"".join(extra) + idat + chunk(b"IEND")


MAC = png(chunk(b"iCCP", b"Display P3\x00\x00x"), chunk(b"eXIf", b"MM\x00*"), chunk(b"iTXt", b"XML:com.adobe.xmp\x00"))


def test_w18_a_macos_screenshot_is_shown_as_evidence():
    got = reads.media(MAC)
    assert got is not None and got["type"] == "png"


def test_w18_an_animated_png_is_still_not_shown():
    assert reads.media(png(chunk(b"acTL", b"\x00" * 8))) is None


def test_w18_a_paste_keeps_the_strict_check():
    assert reads.picture(MAC) is None


def test_w18_a_private_critical_chunk_is_not_shown():
    assert reads.media(png(chunk(b"CgBI", b"\x00" * 4))) is None
