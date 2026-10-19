"""R3 part B: the host's header-only check of a pasted screenshot (no decoder)."""

from __future__ import annotations

import struct
import zlib

import pytest

from orch.host import imagecheck
from orch.host.imagecheck import ImageRefused


def chunk(kind: bytes, body: bytes = b"") -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(*extra: bytes, tail: bytes = b"") -> bytes:
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
    idat = chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
    return b"\x89PNG\r\n\x1a\n" + ihdr + b"".join(extra) + idat + chunk(b"IEND") + tail


def webp(*chunks: tuple[bytes, bytes]) -> bytes:
    body = b"WEBP"
    for kind, data in chunks:
        body += kind + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) & 1 else b"")
    return b"RIFF" + struct.pack("<I", len(body)) + body


def test_a_plain_png_passes():
    assert imagecheck.check(png()) == "image/png"


@pytest.mark.parametrize("kind", [b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"iCCP", b"tIME"])
def test_a_png_with_a_metadata_chunk_is_refused(kind):
    with pytest.raises(ImageRefused, match="metadata"):
        imagecheck.check(png(chunk(kind, b"GPS 47N 8E")))


def test_a_png_with_bytes_after_iend_is_refused():
    with pytest.raises(ImageRefused, match="after its end"):
        imagecheck.check(png(tail=b"<script>alert(1)</script>"))


def test_a_png_that_is_cut_off_or_has_a_chunk_past_the_end_is_refused():
    whole = png()
    with pytest.raises(ImageRefused):
        imagecheck.check(whole[:-6])
    with pytest.raises(ImageRefused):
        imagecheck.check(whole[:20])


def test_a_png_must_start_with_ihdr():
    bad = b"\x89PNG\r\n\x1a\n" + chunk(b"IDAT", b"x") + chunk(b"IEND")
    with pytest.raises(ImageRefused, match="header chunk"):
        imagecheck.check(bad)


def test_svg_jpeg_gif_heic_and_unknown_bytes_are_refused():
    for data, word in (
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script/></svg>', "SVG"),
        (b'<?xml version="1.0"?><svg/>', "SVG"),
        (b"\xff\xd8\xff\xe1\x00\x10Exif\x00\x00GPS", "JPEG"),
        (b"GIF89a\x01\x00\x01\x00", "PNG"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00", "PNG"),
        (b"hello", "PNG"),
    ):
        with pytest.raises(ImageRefused, match=word):
            imagecheck.check(data)


def test_size_caps():
    with pytest.raises(ImageRefused, match="empty"):
        imagecheck.check(b"")
    with pytest.raises(ImageRefused, match="5 MiB"):
        imagecheck.check(png(chunk(b"IDAT", b"\x00" * imagecheck.IMAGE_MAX)))
    exact = png()
    pad = imagecheck.IMAGE_MAX - len(exact) - 12
    ok = png(chunk(b"IDAT", b"\x00" * pad))
    assert len(ok) == imagecheck.IMAGE_MAX
    assert imagecheck.check(ok) == "image/png"


def test_webp_stills_pass_and_metadata_is_refused():
    still = webp((b"VP8 ", b"\x00" * 10))
    assert imagecheck.check(still) == "image/webp"
    assert imagecheck.check(webp((b"VP8L", b"\x2f" + b"\x00" * 9))) == "image/webp"
    for extra in (b"EXIF", b"XMP ", b"ICCP", b"ANIM"):
        with pytest.raises(ImageRefused, match="metadata"):
            imagecheck.check(webp((b"VP8 ", b"\x00" * 10), (extra, b"GPS!")))
    with pytest.raises(ImageRefused, match="metadata"):
        imagecheck.check(webp((b"VP8X", bytes([0x08]) + b"\x00" * 9), (b"VP8 ", b"\x00" * 10)))
    with pytest.raises(ImageRefused, match="size"):
        imagecheck.check(still + b"tail")
