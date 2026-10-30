"""Log text that leaves the helper (HUB-SV-03, #1397).

Redaction happens HERE, before a byte goes back over the socket, so the web layer and the browser never hold a secret
the helper saw. The rules are deliberately broad (a harmless value may be hidden; a secret must not appear):

* control characters (ANSI sequences, bidi and zero-width marks included) are removed; the answer is plain text and the
  console shows it with ``textContent`` only, never as HTML;
* PEM blocks, ``Authorization`` / cookie lines, ``Bearer`` values, ``key=value`` pairs whose key names a secret, URL
  passwords and query strings, and any run of 24+ base64 / hex characters become ``[redacted]``;
* a line is cut at 300 characters, the text at 500 lines and 256 KB (the newest lines are kept)."""

from __future__ import annotations

import re
from collections.abc import Sequence

from .helper_backends import LOG_MAX_BYTES, LOG_TIMEOUT_S, Part, Runner
from .helper_proto import HelperError

MAX_LINES = 500
MAX_LINE = 300
R = "[redacted]"

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL = re.compile(
    "["
    + "\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f"
    + "".join(
        chr(c)
        for c in (
            0x200B,
            0x200C,
            0x200D,
            0x200E,
            0x200F,
            0x202A,
            0x202B,
            0x202C,
            0x202D,
            0x202E,
            0x2060,
            0x2061,
            0x2062,
            0x2063,
            0x2064,
            0x2066,
            0x2067,
            0x2068,
            0x2069,
            0xFEFF,
        )
    )
    + "]"
)
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]+-----.*?(?:-----END [A-Z0-9 ]+-----|\Z)", re.DOTALL)
_HEADER_LINE = re.compile(r"(?i)\b(authorization|proxy-authorization|cookie|set-cookie|x-api-key)\b\s*[:=].*")
_BEARER = re.compile(r"(?i)\b(bearer|basic|token)\s+[A-Za-z0-9._~+/=-]{8,}")
_SECRET_KEY = (
    r"[\w.-]*(?:token|secret|passw|passphrase|totp|api[_-]?key|private|credential|key|cookie|session|auth|"
    r"sig|signature|proof|cert|card|seed|nonce|salt|hash|digest|code|otp|pin)[\w.-]*"
)
_PAIR = re.compile(rf"(?i)(?<![\w.-])({_SECRET_KEY})([\"']?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;&}}\]]+)")
_URL_PW = re.compile(r"(?i)(\b[a-z][a-z0-9+.-]*://[^\s/@:]*:)[^\s/@]+@")
_URL_QS = re.compile(r"(\bhttps?://[^\s?\"']+)\?[^\s\"']*")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+)*")
_LONG = re.compile(r"[A-Za-z0-9+/_=-]{24,}")


def clean_line(line: str) -> str:
    line = _HEADER_LINE.sub(lambda m: f"{m.group(1)}: {R}", line)
    line = _BEARER.sub(lambda m: f"{m.group(1)} {R}", line)
    line = _URL_PW.sub(lambda m: m.group(1) + R + "@", line)
    line = _URL_QS.sub(lambda m: m.group(1) + "?" + R, line)
    line = _PAIR.sub(lambda m: m.group(1) + m.group(2) + R, line)
    line = _JWT.sub(R, line)
    line = _LONG.sub(R, line)
    if len(line) > MAX_LINE:
        line = line[:MAX_LINE] + "…"
    return line


def redact(text: str, *, max_lines: int = MAX_LINES, max_bytes: int = LOG_MAX_BYTES) -> str:
    text = _ANSI.sub("", text).replace("\r", "\n").replace("\t", " ")
    text = _CTRL.sub("", text)
    text = _PEM.sub(R + " (key block)", text)
    lines = [clean_line(x) for x in text.split("\n")]
    lines = [x for x in lines if x.strip()][-max_lines:]
    out = "\n".join(lines)
    raw = out.encode("utf-8")
    if len(raw) > max_bytes:
        out = raw[-max_bytes:].decode("utf-8", "ignore").split("\n", 1)[-1]  # newest bytes, from a line start
    return out


def read_logs(part: Part, runner: Runner) -> str:
    """The part's last hour through the fixed argv, redacted. A failing command says so in a short redacted line."""
    try:
        rc, out, err = runner(part.logs_argv(), LOG_TIMEOUT_S)
    except OSError:
        raise HelperError("action_failed", "could not read the logs") from None
    if rc != 0:
        raise HelperError("action_failed", redact(err, max_lines=2)[:200])
    return redact(out)


__all__: Sequence[str] = ("MAX_LINE", "MAX_LINES", "clean_line", "read_logs", "redact")
