"""Socket contract v1.3.2: framing, envelope validation and connection bookkeeping.

No I/O lives here. Decoder.feed returns objects or terminal FrameErrors in
wire order; validate_envelope checks their shape, and Connection receives client
frames. The transport sends error.response() (hosts only) and honours error.fate.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from time import monotonic
from typing import Any, Literal

from jsonschema import Draft202012Validator, FormatChecker

MAX_FRAME = 2_097_152
MAX_DEPTH = 32
MAX_VALUES = 65_536
MAX_INT = 2**53 - 1
MAX_INFLIGHT = 8
BODY_TIMEOUT_SECONDS = 10
HELLO_TIMEOUT_SECONDS = 2
REQ_ID = r"[A-Za-z0-9_-]{1,32}"
OP_NAME = r"[a-z][a-z_]*(\.[a-z_]+)*"
ULID = r"[0-7][0-9A-HJKMNP-TV-Z]{25}"
HASH = r"sha256:[0-9a-f]{64}"
KEY = r"[A-Z][A-Z0-9]{1,9}-[0-9]{4,}"
_DISPOSITIONS = {
    "conn.overloaded": (9, True, "close"),
    "result.too_large": (1, False, "stays"),
    "internal": (1, False, "stays"),
    "frame.invalid": (6, False, "close"),
    "frame.too_large": (6, False, "close"),
    "handshake.invalid": (6, False, "close"),
    "peer.denied": (3, False, "close"),
    "host.version_mismatch": (1, False, "close"),
    "req.invalid": (6, False, "stays"),
    "quota.host": (9, True, "stays"),
    "cancelled": (9, True, "stays"),
    "outcome.unknown": (9, True, "stays"),
}

_MESSAGES = {
    "conn.overloaded": "Connection resource limit exceeded",
    "result.too_large": "Result exceeds the frame size limit",
    "internal": "Internal host error",
    "frame.invalid": "Invalid frame",
    "frame.too_large": "Frame exceeds the size limit",
    "handshake.invalid": "Invalid handshake",
    "peer.denied": "Peer is not allowed to connect",
    "host.version_mismatch": "No shared protocol version",
    "req.invalid": "Invalid request envelope",
    "quota.host": "Host resource limit exceeded",
    "cancelled": "Request cancelled",
    "outcome.unknown": "Request outcome is unknown",
}


class FrameError(ValueError):
    """A wire error, including its exact §0.6 disposition."""

    def __init__(self, code: str = "frame.invalid", message: str | None = None, request_id: str | None = None):
        super().__init__(_MESSAGES[code] if message is None else message)
        self.code = code
        self.request_id = request_id
        self.exit, self.retryable, self.fate = _DISPOSITIONS[code]
        if code in {"quota.host", "internal"} and request_id is None:
            self.fate = "close"
        if self.fate == "stays" and (type(request_id) is not str or not re.fullmatch(REQ_ID, request_id)):
            raise ValueError("Request-scoped errors require a valid request id")

    def response(self) -> dict[str, Any]:
        error = {"code": self.code, "message": str(self), "retryable": self.retryable}
        if self.fate == "stays":
            return {"t": "res", "id": self.request_id, "ok": False, "exit": self.exit, "error": error}
        return {"t": "fatal", "error": error}


def _reject_number(value: str) -> Any:
    raise FrameError(message="Fractional, exponent and non-finite numbers are forbidden")


def _integer(value: str) -> int:
    # Check length before int(), including Python's own decimal conversion limit.
    digits = value.lstrip("-")
    if len(digits) > 16 or not -MAX_INT <= (number := int(value)) <= MAX_INT:
        raise FrameError(message="Integer outside the safe range")
    return number


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError(message="Duplicate object key")
        result[key] = value
    return result


def _check_json(value: Any, depth: int = 0, *, remaining: list[int] | None = None) -> None:
    if remaining is None:
        remaining = [MAX_VALUES]
    remaining[0] -= 1
    if remaining[0] < 0:
        raise FrameError(message="JSON exceeds 65,536 values")
    if type(value) in (dict, list):
        depth += 1
        if depth > MAX_DEPTH:
            raise FrameError(message="JSON nesting exceeds 32")
        if type(value) is dict:
            for key, item in value.items():
                if type(key) is not str:
                    raise FrameError(message="Object keys must be strings")
                # Member names are not JSON values, but still require valid Unicode.
                if any(0xD800 <= ord(char) <= 0xDFFF for char in key):
                    raise FrameError(message="Lone surrogate")
                _check_json(item, depth, remaining=remaining)
        else:
            for item in value:
                _check_json(item, depth, remaining=remaining)
    elif type(value) is str:
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise FrameError(message="Lone surrogate")
    elif type(value) is int:
        if not -MAX_INT <= value <= MAX_INT:
            raise FrameError(message="Integer outside the safe range")
    elif value is not None and type(value) is not bool:
        raise FrameError(message="Value is not strict JSON")


def decode(body: bytes) -> dict[str, Any]:
    """Decode a complete body. Envelope and connection checks are separate."""
    if len(body) > MAX_FRAME:
        raise FrameError("frame.too_large")
    try:
        value = json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise FrameError() from exc
    if type(value) is not dict:
        raise FrameError(message="Top level must be an object")
    _check_json(value)
    return value


def encode(frame: dict[str, Any]) -> bytes:
    """Encode strict JSON with its length prefix; validate envelopes separately."""
    if type(frame) is not dict:
        raise FrameError(message="Top level must be an object")
    _check_json(frame)
    body = json.dumps(frame, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(body) > MAX_FRAME:
        raise FrameError("frame.too_large")
    return len(body).to_bytes(4, "big") + body


class Decoder:
    """Incremental, bounded buffering; a framing failure permanently stops decoding.

    pending includes partial length prefixes. body_number changes at the first
    byte of each frame, and the deadline includes that byte and the whole prefix.
    The transport calls check_deadline while idle; feed also checks before reads.
    """

    def __init__(self, *, clock: Callable[[], float] = monotonic) -> None:
        self._buffer = bytearray()
        self._clock = clock
        self._deadline: float | None = None
        self.body_length: int | None = None
        self.body_number = 0
        self.failed = False

    @property
    def pending(self) -> bool:
        return self._deadline is not None

    def check_deadline(self) -> None:
        """Reject an unfinished frame after ten seconds, even without new bytes."""
        if not self.failed and self._deadline is not None and self._clock() >= self._deadline:
            self.failed = True
            raise FrameError(message="Frame body deadline exceeded")

    @property
    def body_started(self) -> bool:
        return self.body_length is not None

    @property
    def body_received(self) -> int:
        return len(self._buffer) if self.body_started else 0

    def feed(self, data: bytes) -> list[dict[str, Any] | FrameError]:
        frames: list[dict[str, Any] | FrameError] = []
        try:
            self.check_deadline()
        except FrameError as exc:
            return [exc]
        offset = 0
        while offset < len(data) and not self.failed:
            if not self.pending:
                self.body_number += 1
                self._deadline = self._clock() + BODY_TIMEOUT_SECONDS
            target = self.body_length if self.body_started else 4
            count = min(target - len(self._buffer), len(data) - offset)
            self._buffer.extend(data[offset : offset + count])
            offset += count
            if len(self._buffer) < target:
                continue
            if not self.body_started:
                length = int.from_bytes(self._buffer, "big")
                self._buffer.clear()
                if length == 0 or length > MAX_FRAME:
                    self.failed = True
                    frames.append(FrameError("frame.too_large" if length > MAX_FRAME else "frame.invalid"))
                    return frames
                self.body_length = length
            else:
                body = bytes(self._buffer)
                self._buffer.clear()
                self.body_length = None
                self._deadline = None
                try:
                    frames.append(decode(body))
                except FrameError as exc:
                    self.failed = True
                    frames.append(exc)
        return frames

    def finish(self) -> None:
        """Reject EOF in a partial prefix or body. Clean EOF is not a frame."""
        if self._buffer or self.body_started:
            self.failed = True
            raise FrameError(message="Truncated frame")


# Schemas keep arbitrary op arguments/data opaque; their registries validate them.
def _str(pattern: str | None = None, low: int = 0, high: int | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "string", "minLength": low}
    if pattern is not None:
        schema["pattern"] = rf"^(?:{pattern})(?![\s\S])"
    if high is not None:
        schema["maxLength"] = high
    return schema


def _int(low: int = -MAX_INT, high: int = MAX_INT) -> dict[str, Any]:
    return {"type": "integer", "minimum": low, "maximum": high}


def _array(item: dict[str, Any], high: int, low: int = 0) -> dict[str, Any]:
    return {"type": "array", "items": item, "minItems": low, "maxItems": high}


def _object(required: dict[str, Any], optional: dict[str, Any] | None = None, *, host: bool = False) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": required | (optional or {}),
        "required": list(required),
        # §0.1 applies forward compatibility to all host-sent nested objects too.
        "additionalProperties": host,
    }


def _map(key: str, value: dict[str, Any], high: int | None = None) -> dict[str, Any]:
    schema = {"type": "object", "propertyNames": _str(key), "additionalProperties": value}
    if high is not None:
        schema["maxProperties"] = high
    return schema


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


def _const(value: Any) -> dict[str, Any]:
    return {"const": value, "type": "boolean" if type(value) is bool else "string"}


_STRING = _str()
_BOOL = {"type": "boolean"}
_ERROR = _object(
    {"code": _str(r"[a-z_]+(\.[a-z_]+)*"), "message": _str(low=1, high=400), "retryable": _BOOL},
    {
        "hint": _str(low=1, high=400),
        "fix": _object({"argv": _array(_STRING, 32, 1)}, host=True),
        "retry_after_ms": _int(1, 3_600_000),
        # §0.3 (v1.9): a closed per-code object; a reader ignores unknown keys (§0.1), the host sends only the table's
        "detail": _object(
            {},
            {
                "state": _str(r"[a-z_]+", 1, 32),
                "which": _str(r"[a-z_]+", 1, 32),
                "reason": _str(r"[a-z_]+", 1, 32),
                "missing": _str(r"[a-z_]+", 1, 32),
                "next_key": _str(KEY),
                # H12d-2 (§0.8.6 item 9.9): the readers below the level a reader refusal names
                "devices": _array(_str(r"d_[0-9a-f]{32}"), 64, 0),
            },
            host=True,
        ),
    },
    host=True,
)
_RESULT = _object(
    {
        "data": {},
        "key": _nullable(_str(KEY)),
        "seq": _int(0),
        "cursor": _int(0),
        "hints": _array(_STRING, 8),
        "lines": _array(_STRING, 10_000),
        "duplicate": _BOOL,
        "exit": _int(0, 9),
        "head": _nullable(_STRING),
    },
    host=True,
)
_ITEM_BASE = {"id": _str(r"[0-9]{1,15}\.[evgn]"), "jpos": _int()}
_ITEM = {
    "anyOf": [
        _object(
            _ITEM_BASE
            | {
                "log": {"anyOf": [_str(ULID), _const("workspace")]},
                "key": _nullable(_str(KEY)),
                "uid": _nullable(_str(ULID)),
                "seq": _int(),
                "ws_seq": _int(),
                "type": _STRING,
                "at": _str(r".*Z") | {"format": "date-time"},
                "actor": _object({"kind": _STRING, "id": _nullable(_STRING)}, host=True),
            },
            host=True,
        ),
        _object(_ITEM_BASE | {"uid": _str(ULID), "gone": _const(True)}, host=True),
        _object(_ITEM_BASE | {"type": _const("needs.changed")}, host=True),
        # H11a: a host->runner command on the runner's command stream (D-P2-25), and run lifecycle items (run.sub,
        # D-P2-37): no journal position, their own id letters
        _object(
            {
                "id": _str(r"[0-9]{1,15}\.c"),
                "cmd": _str(r"[0-7][0-9A-HJKMNP-TV-Z]{25}"),
                "op": _str(r"[a-z][a-z.]{0,31}"),
                "args": {"type": "object"},
            },
            host=True,
        ),
        _object({"id": _str(r"[0-9]{1,15}\.r"), "t": _const("run"), "run": {"type": "object"}}, host=True),
        _object(
            {"id": _str(r"[0-9]{1,15}\.r"), "t": _const("gone"), "run": _str(r"r_[0-7][0-9A-HJKMNP-TV-Z]{25}")},
            host=True,
        ),
        # H11c-2: the caller person's runner grant (id, until, scope; never the secret) or null
        _object(
            {
                "id": _str(r"[0-9]{1,15}\.g[0-9]{1,9}"),
                "t": _const("runner"),
                "grant": _nullable(_object({"id": _STRING, "until": _STRING, "scope": _STRING}, host=True)),
            },
            host=True,
        ),
    ]
}
_SCHEMAS = {
    "hello": _object(
        {
            "t": _const("hello"),
            "proto": _array(_int(1, 65535), 8, 1),
            "client": _str(low=1, high=64),
            "caps": _array(_str(r"[a-z][a-z0-9_]{0,23}/[0-9]{1,4}"), 64),
        },
        {"ops": _map(OP_NAME, _int(1, 1000), 256)},
    ),
    "welcome": _object(
        {
            "t": _const("welcome"),
            "proto": _int(),
            "host": _str(low=1, high=64),
            "host_uid": _int(0, 4294967295),
            "workspace": _nullable(_object({"id": _str(r"[0-9a-f]{32}"), "prefix": _str(low=1, high=10)}, host=True)),
            "peer": _object(
                {
                    "uid": _int(),
                    "class": {"enum": ["person", "agent", "worker", "control", "runner"]},
                    "person": _nullable(_str(r"p_[0-9a-f]{32}")),
                },
                host=True,
            ),
            "tier": {"enum": ["A", "B"]},
            "caps": _array(_STRING, 64),
            "ops": _map(OP_NAME, _int()),
            "registry": _str(HASH),
            "limits": _object(
                {key: _int() for key in ("frame", "inflight", "timeout_ms", "blob_bytes", "inline_bytes")}, host=True
            ),
        },
        host=True,
    ),
    "req": _object(
        {"t": _const("req"), "id": _str(REQ_ID), "op": _str(OP_NAME), "args": {"type": "object"}},
        {
            "auth": _object({}, {"grant": _str(low=1, high=128), "session": _str(low=1, high=64)}),
            "opts": _object({}, {"dry_run": _BOOL, "idem": _str(r"a_" + ULID), "timeout_ms": _int(1000, 600_000)}),
            "files": _map(
                r"[a-z][a-z_]{0,31}",
                {
                    "oneOf": [
                        _object({"b64": _str(r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?")}),
                        _object({"blob": _str(r"b_[A-Z2-7]{26}")}),
                        _object({"digest": _str(HASH)}),
                    ]
                },
                16,
            ),
        },
    ),
    "res": {
        "oneOf": [
            _object({"t": _const("res"), "id": _str(REQ_ID), "ok": _const(True), "result": _RESULT}, host=True),
            _object(
                {"t": _const("res"), "id": _str(REQ_ID), "ok": _const(False), "exit": _int(1, 9), "error": _ERROR},
                host=True,
            ),
        ]
    },
    "evt": _object(
        {
            "t": _const("evt"),
            "sub": _str(r"sub_[0-9a-z]{1,16}"),
            "cursor": _str(low=1, high=256),
            "items": _array(_ITEM, 64),
        },
        {"end": _STRING},
        host=True,
    ),
    "fatal": _object({"t": _const("fatal"), "error": _ERROR}, host=True),
    "cancel": _object({"t": _const("cancel"), "id": _STRING}),
}
_FORMATS = FormatChecker()


@_FORMATS.checks("date-time")
def _utc_time(value: Any) -> bool:
    # jsonschema's RFC 3339 checker needs an optional dependency; stay stdlib-only.
    if not isinstance(value, str):
        return True
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[Tt][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?Z", value):
        return False
    # RFC 3339 permits a leap second; datetime does not represent one.
    normalized = value[:17] + "59" + value[19:] if value[17:19] == "60" else value
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


_VALIDATORS = {name: Draft202012Validator(schema, format_checker=_FORMATS) for name, schema in _SCHEMAS.items()}


def _readable_id(frame: dict[str, Any]) -> str | None:
    value = frame.get("id")
    # "Readable" means a valid ReqId, not merely an arbitrary JSON string.
    return value if type(value) is str and re.fullmatch(REQ_ID, value) else None


def validate_envelope(frame: dict[str, Any], *, sender: Literal["client", "host"] | None = None) -> None:
    """Check §0.3 shape without op schemas, negotiation, quotas or I/O."""
    if type(frame) is not dict:
        raise FrameError()
    _check_json(frame)
    kind = frame.get("t")
    if type(kind) is not str or kind not in _VALIDATORS:
        raise FrameError(message="Unknown frame type")
    if sender is not None and (kind in {"hello", "req", "cancel"}) != (sender == "client"):
        raise FrameError(message="Wrong frame direction")
    if not _VALIDATORS[kind].is_valid(frame):
        request_id = _readable_id(frame)
        if kind == "req" and request_id is not None:
            raise FrameError("req.invalid", "Invalid request envelope", request_id)
        # A malformed hello is a handshake error; malformed cancel has no request response.
        raise FrameError("handshake.invalid" if kind == "hello" else "frame.invalid", "Invalid envelope")


@dataclass(eq=False)
class Request:
    id: str
    phase: Literal["pending", "queued", "read", "wait", "write", "rejected"] = "pending"
    running: bool = False
    answered: bool = False
    cancelled: bool = False


class Connection:
    """Host-side lifecycle, with generation-specific request handles.

    receive reserves IDs; request retrieves the handle for a malformed request.
    Dispatch calls admit after op, version, drain and permission checks, then
    start before execution. Call responded after sending the one res, and stopped
    only when the handler actually exits (including after cancellation).
    """

    def __init__(self) -> None:
        self.phase: Literal["hello", "welcome", "ready", "closed"] = "hello"
        self._requests: dict[str, Request] = {}
        self._slots: set[Request] = set()

    @property
    def inflight(self) -> frozenset[str]:
        return frozenset(self._requests)

    @property
    def slots(self) -> int:
        return len(self._slots)

    def request(self, request_id: str) -> Request:
        try:
            return self._requests[request_id]
        except KeyError:
            raise ValueError("Request is not in flight") from None

    def welcomed(self) -> None:
        if self.phase != "welcome":
            raise ValueError("No hello awaiting welcome")
        self.phase = "ready"

    def receive(self, frame: dict[str, Any]) -> Request | str | None:
        try:
            return self._receive(frame)
        except FrameError as exc:
            if exc.fate == "close":
                self.phase = "closed"
            raise

    def _receive(self, frame: dict[str, Any]) -> Request | str | None:
        if self.phase == "closed":
            raise ValueError("Connection is closed")
        if type(frame) is not dict:
            raise FrameError()
        kind = frame.get("t")
        if (
            (self.phase == "hello" and kind != "hello")
            or self.phase == "welcome"
            or (self.phase == "ready" and kind == "hello")
        ):
            raise FrameError("handshake.invalid")
        request_id = _readable_id(frame)
        if kind == "req" and request_id in self._requests:
            raise FrameError(message="Request id is already in flight")
        try:
            validate_envelope(frame, sender="client")
        except FrameError as exc:
            if exc.code == "req.invalid":
                self._requests[request_id] = Request(request_id, "rejected")
                # H1b must apply outbound backpressure: malformed requests skip the in-flight limit.
            raise
        if kind == "hello":
            self.phase = "welcome"
        elif kind == "req":
            request = Request(request_id)
            self._requests[request_id] = request
            return request
        elif kind == "cancel":
            return self.cancel(frame["id"])
        return None

    def _current(self, request: Request) -> bool:
        return isinstance(request, Request) and self._requests.get(request.id) is request

    def admit(self, request: Request) -> bool:
        """Reserve quota after the dispatcher's higher-precedence checks."""
        if not self._current(request) or request.phase != "pending":
            raise ValueError("Invalid request transition")
        if request.cancelled:
            return False
        if self.slots >= MAX_INFLIGHT:
            request.phase = "rejected"
            raise FrameError("quota.host", "Too many requests in flight", request.id)
        self._slots.add(request)
        request.phase = "queued"
        return True

    def start(self, request: Request, kind: Literal["read", "wait", "write"]) -> bool:
        if not self._current(request) or kind not in {"read", "wait", "write"}:
            raise ValueError("Invalid request transition")
        if request.cancelled:
            return False
        if request.phase != "queued":
            raise ValueError("Invalid request transition")
        request.phase = kind
        request.running = True
        return True

    def cancel(self, request_id: str) -> str | None:
        request = self._requests.get(request_id)
        if request is None or request.phase == "rejected" or request.cancelled:
            return None
        request.cancelled = True
        return "outcome.unknown" if request.phase == "write" else "cancelled"

    def responded(self, request: Request) -> bool:
        """Release the wire ID once; stale generations never touch its replacement."""
        if not self._current(request):
            return False
        del self._requests[request.id]
        request.answered = True
        if not request.running:
            self._slots.discard(request)
        return True

    def stopped(self, request: Request) -> None:
        """Release execution capacity only after the handler has actually stopped."""
        if request not in self._slots:
            return
        request.running = False
        if request.answered:
            self._slots.discard(request)
