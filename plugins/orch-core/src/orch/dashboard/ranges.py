"""A response for bytes already read and verified, honouring one Range header (RFC 9110) so a browser can seek in a
video. Every caller passes its own security headers; they go on the 200, 206 and 416 alike."""
from __future__ import annotations

import re

from fastapi import Request
from fastapi.responses import Response

_RANGE = re.compile(r"bytes=(\d*)-(\d*)")


def ranged(request: Request, data: bytes, media: str, headers: dict) -> Response:
    """200 with the whole body; 206 for one satisfiable range (`a-b`, `a-`, `-n`); 416 when it lies past the end.
    Several ranges, or a header that is not a byte range, get the whole body, which the RFC allows."""
    size = len(data)
    headers = {**headers, "Accept-Ranges": "bytes"}
    m = _RANGE.fullmatch(request.headers.get("range", "").strip())
    if not m or not (m[1] or m[2]):
        return Response(data, media_type=media, headers=headers)
    if m[1]:
        start, end = int(m[1]), int(m[2]) if m[2] else size - 1
        if m[2] and end < start:  # not a valid range: ignored, the whole body
            return Response(data, media_type=media, headers=headers)
        end = min(end, size - 1)
    else:  # -n: the last n bytes
        start, end = max(size - int(m[2]), 0), size - 1
    if start >= size or not m[1] and int(m[2]) == 0:  # past the end, or "-0"
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"})
    return Response(data[start:end + 1], status_code=206, media_type=media,
                    headers={**headers, "Content-Range": f"bytes {start}-{end}/{size}"})
