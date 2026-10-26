"""The repository config, read as data (D-P2-19, F7).

A strict parser of git's config syntax that keeps three things: ``core.repositoryformatversion``, every
``extensions.*`` key and the raw values of ``remote.origin.url``. ``include`` and ``includeIf`` are never followed and
nothing else is acted on (hooks, filters, ``core.fsmonitor``, ``core.worktree``, ``url.*.insteadOf``, ...). A line
the parser does not understand refuses the repository.

**Extensions (F7).** The allowed set is closed: ``objectFormat`` (``sha1``, ``sha256``) and ``noop``. Any other
extension (``worktreeConfig``, ``refStorage``, ``partialClone``, ...) refuses the repository. A
``repositoryformatversion`` other than 0 or 1 refuses it; with version 0 only ``noop`` and ``objectFormat = sha1``
pass (git ignores version-0 extensions, so a different reading would be a different repository).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import Unavailable

__all__ = ["RepoConfig", "parse", "read_config"]

ALLOWED_EXTENSIONS = {"objectformat", "noop"}
_ESCAPES = {ord("n"): "\n", ord("t"): "\t", ord("b"): "\b", ord('"'): '"', ord("\\"): "\\"}


@dataclass
class RepoConfig:
    version: int = 0
    object_format: str = "sha1"
    origin_urls: list[str] = field(default_factory=list)


def _bad(lineno: int, what: str) -> Unavailable:
    return Unavailable(f"config line {lineno}: {what}")


def _is_key_char(c: str) -> bool:
    return c.isascii() and (c.isalnum() or c == "-")


def parse(data: bytes) -> list[tuple[str, str | None, str, str | None]]:
    """``[(section, subsection, key, value)]`` in file order. Section and key lower-cased, subsection as written
    (``[a.b]`` legacy form: lower-cased), value ``None`` for a bare key (boolean true)."""
    text = data.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    text = text.replace("\r\n", "\n")  # git drops a CR before LF
    out: list[tuple[str, str | None, str, str | None]] = []
    n = len(text)
    i = 0
    lineno = 1
    section: str | None = None
    sub: str | None = None

    def skip_ws(j: int) -> int:
        while j < n and text[j] in " \t\r":
            j += 1
        return j

    while i < n:
        i = skip_ws(i)
        if i >= n:
            break
        c = text[i]
        if c == "\n":
            i += 1
            lineno += 1
            continue
        if c in "#;":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == "[":
            i += 1
            start = i
            while i < n and (_is_key_char(text[i]) or text[i] == "."):
                i += 1
            name = text[start:i]
            if not name:
                raise _bad(lineno, "empty section name")
            if i < n and text[i] == "]":
                i += 1
                if "." in name:
                    head, _, rest = name.partition(".")
                    section, sub = head.lower(), rest.lower()
                else:
                    section, sub = name.lower(), None
            elif i < n and text[i] in " \t":
                if "." in name:
                    raise _bad(lineno, "bad section header")
                i = skip_ws(i)
                if i >= n or text[i] != '"':
                    raise _bad(lineno, "bad section header")
                i += 1
                buf = []
                while True:
                    if i >= n or text[i] == "\n":
                        raise _bad(lineno, "unterminated subsection")
                    ch = text[i]
                    if ch == '"':
                        i += 1
                        break
                    if ch == "\\":
                        i += 1
                        if i >= n or text[i] == "\n":
                            raise _bad(lineno, "unterminated subsection")
                        ch = text[i]
                    buf.append(ch)
                    i += 1
                if i >= n or text[i] != "]":
                    raise _bad(lineno, "bad section header")
                i += 1
                section, sub = name.lower(), "".join(buf)
            else:
                raise _bad(lineno, "bad section header")
            continue
        # a key
        if not (c.isascii() and c.isalpha()):
            raise _bad(lineno, "a key must start with a letter")
        if section is None:
            raise _bad(lineno, "a key outside any section")
        start = i
        while i < n and _is_key_char(text[i]):
            i += 1
        key = text[start:i].lower()
        i = skip_ws(i)
        if i >= n or text[i] == "\n" or text[i] in "#;":
            out.append((section, sub, key, None))
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i] != "=":
            raise _bad(lineno, "expected '=' after a key")
        i = skip_ws(i + 1)
        value: list[str] = []
        quoted = False
        trailing = 0  # whitespace appended outside quotes, dropped at the end of the value
        while True:
            if i >= n:
                if quoted:
                    raise _bad(lineno, "unterminated quote")
                break
            ch = text[i]
            if ch == "\n":
                if quoted:
                    raise _bad(lineno, "unterminated quote")
                break
            if not quoted and ch in "#;":
                while i < n and text[i] != "\n":
                    i += 1
                break
            if ch == "\\":
                i += 1
                if i >= n:
                    raise _bad(lineno, "a backslash at the end of the file")
                e = text[i]
                if e == "\n":  # continuation
                    lineno += 1
                    i += 1
                    continue
                rep = _ESCAPES.get(ord(e))
                if rep is None:
                    raise _bad(lineno, "unknown escape")
                value.append(rep)
                trailing = 0
                i += 1
                continue
            if ch == '"':
                quoted = not quoted
                trailing = 0
                i += 1
                continue
            if not quoted and ch in " \t\r":
                value.append(" ")
                trailing += 1
                i += 1
                continue
            value.append(ch)
            trailing = 0
            i += 1
        if trailing:
            del value[len(value) - trailing :]
        out.append((section, sub, key, "".join(value)))
    return out


def read_config(data: bytes) -> RepoConfig:
    cfg = RepoConfig()
    version: str | None = None
    ext: dict[str, str | None] = {}
    urls: list[str] = []
    for section, sub, key, value in parse(data):
        if section == "core" and sub is None and key == "repositoryformatversion":
            version = value
        elif section == "extensions" and sub is None:
            ext[key] = value
        elif section == "remote" and sub == "origin" and key == "url":
            if value is None:
                raise Unavailable("remote.origin.url has no value")
            urls.append(value)
    if version is None:
        cfg.version = 0
    elif version.strip() in ("0", "1"):
        cfg.version = int(version.strip())
    else:
        raise Unavailable(f"core.repositoryformatversion {version!r} is not supported")
    for name, value in ext.items():
        if name not in ALLOWED_EXTENSIONS:
            raise Unavailable(f"the extension {name!r} is not supported")
        if name == "objectformat":
            if value not in ("sha1", "sha256"):
                raise Unavailable(f"extensions.objectFormat {value!r} is not supported")
            if cfg.version == 0 and value != "sha1":
                raise Unavailable("extensions.objectFormat needs core.repositoryformatversion 1")
            cfg.object_format = value
    cfg.origin_urls = urls
    return cfg
