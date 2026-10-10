"""Text and JSON renderers, the result envelope and the error envelope (format doc 10.4).

* Text: the first line is ``ok <KEY> <event> <detail> seq=<n>`` (the operation's template), then the result's
  extra lines, then at most one ``next:`` line. A retried call adds `` duplicate`` to the first line.
* JSON (``--json`` or ``ORCH_OUTPUT=json``): ``{"v":"orch.cli/2.0","ok":true,"data":...}`` on stdout, compact.
* Errors: text goes to stderr as ``err <code> <message> · retry:<bool> · next: <hint> · fix: <command>``; with
  ``--json`` the envelope ``{"ok":false,"error":{...}}`` goes to stdout, so one stream carries one JSON document.
* No colour unless the stream is a terminal, ``NO_COLOR`` is unset and the output is not JSON.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterable, Mapping
from typing import Any

from orch.ops import Operation, Result
from orch.ops.errors import ERRORS, OrchError

__all__ = [
    "CLI_VERSION",
    "error_envelope",
    "error_exit",
    "error_text",
    "fill",
    "dumps",
    "redact",
    "result_envelope",
    "result_text",
    "use_color",
]

CLI_VERSION = "orch.cli/2.0"
_GROUP = re.compile(r"\[([^\[\]\n]*)\]")
_FIELD = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


class TemplateError(ValueError):
    """A required field of an output template is missing from the result."""


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def _one_line(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(_one_line(v) for v in value)
    return " ".join(str(value).split())


def _subst(text: str, fields: Mapping[str, Any]) -> str | None:
    """Fill ``{name}``; ``None`` when a field is missing, ``None`` or empty."""
    missing = False

    def one(m: re.Match[str]) -> str:
        nonlocal missing
        v = fields.get(m.group(1))
        if v is None or v == "" or v == []:
            missing = True
            return ""
        return _one_line(v)

    out = _FIELD.sub(one, text)
    return None if missing else out


def fill(template: str, fields: Mapping[str, Any]) -> str:
    """Render an output template. ``[ text {x}]`` is dropped when ``x`` is absent; a ``next:`` line is dropped
    when ``next`` is absent; any other missing field is a :class:`TemplateError`."""
    lines = template.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if i == 0:
            line = _GROUP.sub(lambda m: _subst(m.group(1), fields) or "", line)
            filled = _subst(line, fields)
            if filled is None:
                raise TemplateError(f"missing field in {line!r}")
            out.append(filled)
        else:
            filled = _subst(line, fields)
            if filled is not None:
                out.append(filled)
    return "\n".join(out)


def _fields(res: Result) -> dict[str, Any]:
    f: dict[str, Any] = dict(res.data) if isinstance(res.data, dict) else {}
    f.setdefault("key", res.key)
    f.setdefault("seq", res.seq)
    f.setdefault("cursor", res.cursor)
    f["next"] = res.hints[0] if res.hints else None
    return f


def result_text(op: Operation, res: Result) -> str:
    head, _, tail = fill(op.output["text"], _fields(res)).partition("\n")
    if res.duplicate:
        head += " duplicate"
    parts = [head, *res.lines]
    if tail:
        parts.append(tail)
    return "\n".join(parts)


def result_envelope(res: Result) -> dict[str, Any]:
    env: dict[str, Any] = {
        "v": CLI_VERSION,
        "ok": True,
        "data": res.data,
        "key": res.key,
        "seq": res.seq,
        "cursor": res.cursor,
        "hints": list(res.hints),
    }
    if res.duplicate:
        env["duplicate"] = True
    return env


def _declared(op: Operation | None, code: str) -> dict[str, Any] | None:
    if op is None:
        return None
    return next((e for e in op.errors if e["code"] == code), None)


def error_envelope(err: OrchError, op: Operation | None = None) -> dict[str, Any]:
    """Resolve hint, fix and retryable: what the error carries, else the operation's declared entry, else the
    catalog's. ``{cmd}`` in a catalog hint or fix stands for the operation."""
    cat = ERRORS.get(err.code)
    decl = _declared(op, err.code)
    name = op.name if op else "describe"
    hint = err.hint
    fix = err.fix
    retryable = err.retryable
    if decl:
        hint = hint or decl["hint"]
        fix = fix or decl["fix"]["argv"]
        retryable = decl.get("retryable") if retryable is None else retryable
    if cat:
        hint = hint or cat.hint.replace("{cmd}", name)
        fix = fix or [a.replace("{cmd}", name) for a in cat.fix]
        retryable = cat.retryable if retryable is None else retryable
    body: dict[str, Any] = {"code": err.code, "message": err.message}
    if hint:
        body["hint"] = hint
    if fix:
        body["fix"] = {"argv": list(fix)}
    body["retryable"] = bool(retryable)
    return {"ok": False, "error": body}


def error_exit(code: str) -> int:
    cat = ERRORS.get(code)
    return cat.exit if cat else 1


def use_color(stream: Any, as_json: bool, env: Mapping[str, str]) -> bool:
    if as_json or env.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def error_text(env: Mapping[str, Any], color: bool = False) -> str:
    e = env["error"]
    label = "\x1b[1;31merr\x1b[0m" if color else "err"
    parts = [f"{label} {e['code']} {_one_line(e['message'])}", f"retry:{'true' if e['retryable'] else 'false'}"]
    if e.get("hint"):
        parts.append(f"next: {_one_line(e['hint'])}")
    if e.get("fix"):
        fix = shlex.join(e["fix"]["argv"])
        if fix != e.get("hint"):
            parts.append(f"fix: {fix}")
    return " · ".join(parts)


def redact(text: str, secrets: Iterable[str]) -> str:
    """Remove secret values (the grant secret) from anything about to be printed."""
    for s in secrets:
        if s and len(s) >= 8:
            text = text.replace(s, "[redacted]")
    return text
