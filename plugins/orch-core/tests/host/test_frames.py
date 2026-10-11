"""Contract v1.3 framing, envelopes and request lifecycle."""

import copy
import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from orch.host.frames import (
    BODY_TIMEOUT_SECONDS,
    MAX_FRAME,
    MAX_INT,
    MAX_VALUES,
    Connection,
    Decoder,
    FrameError,
    decode,
    encode,
    validate_envelope,
)

HELLO = {"t": "hello", "proto": [1], "client": "test", "caps": []}
REQ = {"t": "req", "id": "r1", "op": "ticket.get", "args": {}}
ERROR = {"code": "frame.invalid", "message": "Invalid frame", "retryable": False}
RESULT = {
    "data": None,
    "key": None,
    "seq": 0,
    "cursor": 0,
    "hints": [],
    "lines": [],
    "duplicate": False,
    "exit": 0,
    "head": None,
}
WELCOME = {
    "t": "welcome",
    "proto": 1,
    "host": "orch",
    "host_uid": 1000,
    "workspace": None,
    "peer": {"uid": 1000, "class": "control", "person": None},
    "tier": "A",
    "caps": [],
    "ops": {},
    "registry": "sha256:" + "0" * 64,
    "limits": {"frame": MAX_FRAME, "inflight": 8, "timeout_ms": 30000, "blob_bytes": 67108864, "inline_bytes": 524288},
}
ENVELOPES = [
    HELLO,
    WELCOME,
    REQ,
    {"t": "res", "id": "r1", "ok": True, "result": RESULT},
    {"t": "res", "id": "r1", "ok": False, "exit": 6, "error": ERROR},
    {"t": "evt", "sub": "sub_a", "cursor": "c", "items": []},
    {"t": "fatal", "error": ERROR},
    {"t": "cancel", "id": "r1"},
]


def wire(body: bytes) -> bytes:
    return len(body).to_bytes(4, "big") + body


def ready() -> Connection:
    connection = Connection()
    connection.receive(copy.deepcopy(HELLO))
    connection.welcomed()
    return connection


def assert_error(code, function, *args, **kwargs):
    with pytest.raises(FrameError) as caught:
        function(*args, **kwargs)
    error = caught.value
    assert error.code == code
    assert error.fate == ("stays" if code in {"req.invalid", "quota.host"} else "close")
    assert error.exit == (9 if code == "quota.host" else 6)
    assert error.retryable is (code == "quota.host")
    validate_envelope(error.response(), sender="host")
    return error


@pytest.mark.parametrize("frame", ENVELOPES)
def test_envelopes(frame):
    validate_envelope(frame)
    assert decode(encode(frame)[4:]) == frame


@pytest.mark.parametrize("frame", ENVELOPES)
def test_all_required_fields(frame):
    for key in frame:
        malformed = copy.deepcopy(frame)
        del malformed[key]
        with pytest.raises(FrameError):
            validate_envelope(malformed)


@pytest.mark.parametrize("frame", ENVELOPES)
def test_required_fields_are_not_implicitly_nullable(frame):
    for key in frame:
        if frame[key] is None:
            continue
        malformed = copy.deepcopy(frame)
        malformed[key] = None
        with pytest.raises(FrameError):
            validate_envelope(malformed)


def test_partial_and_coalesced():
    data = encode(HELLO) + encode(REQ) + encode({"t": "cancel", "id": "r1"})
    expected = [HELLO, REQ, {"t": "cancel", "id": "r1"}]
    for split in range(len(data) + 1):
        decoder = Decoder()
        assert list(decoder.feed(data[:split])) + list(decoder.feed(data[split:])) == expected
        decoder.finish()
    decoder = Decoder()
    assert [frame for byte in data for frame in decoder.feed(bytes([byte]))] == expected


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"\xff",
        b'{"x":"\xff"}',
        b'{"x":"\\ud800"}',
        b'{"\\udfff":0}',
        b'{"x":"\\ud800x\\udc00"}',
        b'{"x":1,"x":2}',
        b'{"x":{"y":1,"y":2}}',
        b'{"x":1,"\\u0078":2}',
        b'{"x":1.0}',
        b'{"x":1e0}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":9007199254740992}',
        b'{"x":-9007199254740992}',
        b'{"x":' + b"1" * 5000 + b"}",
        b"[]",
        b"null",
        b'"text"',
        b"1",
        b"true",
        b"{}{}",
        b"{} trailing",
        b'{"x":}',
        b"\xef\xbb\xbf{}",
        b'{"x":"\xed\xa0\x80"}',
    ],
)
def test_invalid_json(body):
    assert_error("frame.invalid", decode, body)
    frames = list(Decoder().feed(wire(body)))
    assert len(frames) == 1 and isinstance(frames[0], FrameError)
    assert frames[0].code == "frame.invalid"


def test_valid_unicode_and_integer_bounds():
    assert decode(b'{"x":"\\ud83d\\ude00"}') == {"x": "😀"}
    frame = {"é": [MAX_INT, -MAX_INT, True, None, "😀"]}
    assert decode(encode(frame)[4:]) == frame


@pytest.mark.parametrize("container", [lambda x: {"x": x}, lambda x: [x]])
def test_depth(container):
    value = 0
    for _ in range(31):
        value = container(value)
    frame = {"x": value}
    assert decode(encode(frame)[4:]) == frame
    frame = {"x": frame}
    assert_error("frame.invalid", decode, json.dumps(frame).encode())
    assert_error("frame.invalid", encode, frame)
    assert_error("frame.invalid", decode, b'{"x":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}")


@pytest.mark.parametrize(
    "value", [1.0, float("nan"), float("inf"), MAX_INT + 1, -MAX_INT - 1, "\ud800", {1: "key"}, (1, 2), b"bytes"]
)
def test_encoder_rejects_non_json(value):
    assert_error("frame.invalid", encode, {"x": value})


def test_lengths_and_terminal_errors():
    body = b'{"x":"' + b"x" * (MAX_FRAME - 8) + b'"}'
    assert len(body) == MAX_FRAME
    assert len(encode(decode(body))) == MAX_FRAME + 4
    assert_error("frame.too_large", encode, {"x": "x" * MAX_FRAME})
    assert_error("frame.too_large", decode, body + b" ")
    for length, code in [(0, "frame.invalid"), (MAX_FRAME + 1, "frame.too_large"), (2**32 - 1, "frame.too_large")]:
        decoder = Decoder()
        frames = list(decoder.feed(encode(HELLO) + length.to_bytes(4, "big") + encode(REQ)))
        assert frames[0] == HELLO
        assert frames[1].code == code
        assert len(frames) == 2 and decoder.failed
        assert list(decoder.feed(encode(REQ))) == []
    decoder = Decoder()
    frames = list(decoder.feed(encode(HELLO) + wire(b"[]") + encode(REQ)))
    assert frames[0] == HELLO and frames[1].code == "frame.invalid" and len(frames) == 2


def test_body_deadline_state():
    assert BODY_TIMEOUT_SECONDS == 10
    decoder = Decoder()
    data = encode(REQ)
    assert list(decoder.feed(data[:3])) == []
    assert not decoder.body_started
    assert list(decoder.feed(data[3:4])) == []
    assert decoder.body_started and decoder.body_received == 0
    assert decoder.body_length == len(data) - 4 and decoder.body_number == 1
    assert list(decoder.feed(data[4:6])) == []
    assert decoder.body_received == 2
    assert list(decoder.feed(data[6:] + data[:5])) == [REQ]
    assert decoder.body_started and decoder.body_number == 2 and decoder.body_received == 1
    assert list(decoder.feed(data[5:])) == [REQ]
    assert not decoder.body_started and decoder.body_received == 0
    decoder.finish()


@pytest.mark.parametrize("partial", [b"\x00", b"\x00\x00\x00\x02", b"\x00\x00\x00\x02{"])
def test_eof(partial):
    decoder = Decoder()
    list(decoder.feed(partial))
    assert_error("frame.invalid", decoder.finish)
    assert decoder.failed


@pytest.mark.parametrize(
    "patch",
    [
        {"extra": 1},
        {"op": "Invalid"},
        {"args": []},
        {"auth": None},
        {"opts": None},
        {"files": None},
        {"auth": {"grant": ""}},
        {"auth": {"session": None}},
        {"auth": {"extra": 1}},
        {"opts": {"dry_run": 1}},
        {"opts": {"timeout_ms": True}},
        {"opts": {"timeout_ms": 999}},
        {"opts": {"timeout_ms": 600001}},
        {"opts": {"idem": "a_bad"}},
        {"opts": {"extra": 1}},
        {"files": {"Bad": {"b64": ""}}},
        {"files": {"path": {"b64": "YQ"}}},
        {"files": {"path": {"b64": "YQ==\n"}}},
        {"files": {"path": {"blob": "bad"}}},
        {"files": {"path": {"digest": "sha256:bad"}}},
        {"files": {"path": {"b64": "", "blob": "b_" + "A" * 26}}},
    ],
)
def test_bad_request_envelopes(patch):
    error = assert_error("req.invalid", validate_envelope, REQ | patch)
    assert error.request_id == "r1"


@pytest.mark.parametrize("request_id", [None, 1, True, "", "x" * 33, "hello world", "é", "id\n"])
def test_unreadable_ids(request_id):
    assert_error("frame.invalid", validate_envelope, REQ | {"id": request_id})
    if not isinstance(request_id, str):
        assert_error("frame.invalid", validate_envelope, {"t": "cancel", "id": request_id})


def test_optional_and_nested_shapes():
    validate_envelope(REQ | {"id": "a" * 32, "args": {"anything": [1]}, "auth": {}, "opts": {}, "files": {}})
    validate_envelope(
        REQ
        | {
            "opts": {"timeout_ms": 600000, "dry_run": False, "idem": "a_" + "0" * 26},
            "files": {"a": {"b64": "YQ=="}, "b": {"blob": "b_" + "A" * 26}, "c": {"digest": "sha256:" + "a" * 64}},
        }
    )
    for path in ("key", "head"):
        validate_envelope(ENVELOPES[3] | {"result": RESULT | {path: None}})
    for key in RESULT:
        result = RESULT.copy()
        del result[key]
        assert_error("frame.invalid", validate_envelope, ENVELOPES[3] | {"result": result})
    for result in [
        RESULT | {"seq": -1},
        RESULT | {"cursor": True},
        RESULT | {"hints": [""] * 9},
        RESULT | {"lines": [""] * 10001},
        RESULT | {"exit": 10},
        RESULT | {"key": "bad"},
    ]:
        assert_error("frame.invalid", validate_envelope, ENVELOPES[3] | {"result": result})


@pytest.mark.parametrize(
    "patch",
    [
        {"proto": []},
        {"proto": [1] * 9},
        {"proto": [True]},
        {"proto": [0]},
        {"proto": [65536]},
        {"client": ""},
        {"client": "a" * 65},
        {"caps": ["BAD/1"]},
        {"caps": ["a/1"] * 65},
        {"ops": {"a": 1001}},
        {"ops": {"Bad": 1}},
        {"extra": 0},
    ],
)
def test_invalid_hello(patch):
    assert_error("handshake.invalid", validate_envelope, HELLO | patch)


def test_unknown_host_fields_and_direction():
    for frame in ENVELOPES:
        if frame["t"] in {"hello", "req", "cancel"}:
            with pytest.raises(FrameError):
                validate_envelope(frame | {"future": 1}, sender="client")
            assert_error("frame.invalid", validate_envelope, frame, sender="host")
        else:
            validate_envelope(frame | {"future": 1}, sender="host")
            assert_error("frame.invalid", validate_envelope, frame, sender="client")
    validate_envelope(WELCOME | {"peer": WELCOME["peer"] | {"future": True}})
    validate_envelope({"t": "fatal", "error": ERROR | {"future": True}})
    validate_envelope(ENVELOPES[5] | {"end": "future-reason"})
    assert_error("frame.invalid", validate_envelope, {"t": []})
    assert_error("frame.invalid", validate_envelope, {"t": "future"})


def test_events():
    uid = "0" * 26
    items = [
        {
            "id": "1.e",
            "jpos": 1,
            "log": "workspace",
            "key": None,
            "uid": None,
            "seq": 0,
            "ws_seq": 1,
            "type": "changed",
            "at": "2026-10-11T12:00:00Z",
            "actor": {"kind": "host", "id": None},
        },
        {"id": "1.g", "jpos": 1, "uid": uid, "gone": True},
        {"id": "1.n", "jpos": 1, "type": "needs.changed"},
    ]
    validate_envelope(ENVELOPES[5] | {"items": items})
    validate_envelope(ENVELOPES[5] | {"items": [item | {"future": 1} for item in items]})
    for patch in [
        {"sub": "bad"},
        {"cursor": ""},
        {"items": [items[0]] * 65},
        {"end": None},
        {"items": [items[0] | {"at": "2026-99-11T12:00:00Z"}]},
        {"items": [items[1] | {"gone": False}]},
        {"items": [items[2] | {"id": "bad"}]},
    ]:
        assert_error("frame.invalid", validate_envelope, ENVELOPES[5] | patch)


def test_id_reuse_and_out_of_order_responses():
    connection = ready()
    connection.receive(REQ)
    connection.receive(REQ | {"id": "r2"})
    connection.responded(connection.request("r2"))
    connection.receive(REQ | {"id": "r2"})
    connection.responded(connection.request("r1"))
    connection.receive(REQ)
    assert_error("frame.invalid", connection.receive, REQ | {"extra": 1})
    assert connection.phase == "closed"


def test_malformed_request_stays_open_until_error_sent():
    connection = ready()
    assert_error("req.invalid", connection.receive, REQ | {"extra": True})
    assert connection.inflight == {"r1"} and connection.phase == "ready"
    assert connection.receive({"t": "cancel", "id": "r1"}) is None
    with pytest.raises(ValueError):
        connection.start(connection.request("r1"), "write")
    connection.responded(connection.request("r1"))
    connection.receive(REQ)
    connection.responded(connection.request("r1"))
    with pytest.raises(ValueError):
        connection.responded(connection.request("r1"))


@pytest.mark.parametrize("kind", ["queued", "read", "wait", "write"])
def test_cancel(kind):
    connection = ready()
    cancel = {"t": "cancel", "id": "r1"}
    assert connection.receive(cancel) is None
    connection.receive(REQ)
    connection.admit(connection.request("r1"))
    if kind != "queued":
        assert connection.start(connection.request("r1"), kind)
    expected = "outcome.unknown" if kind == "write" else "cancelled"
    assert connection.receive(cancel) == expected
    assert connection.receive(cancel) is None
    assert connection.inflight == {"r1"}
    if kind == "queued":
        assert not connection.start(connection.request("r1"), "write")
    connection.responded(connection.request("r1"))
    assert connection.receive(cancel) is None
    connection.receive(REQ)
    connection.admit(connection.request("r1"))
    assert connection.start(connection.request("r1"), "write")


def test_cancel_does_not_free_id():
    connection = ready()
    connection.receive(REQ)
    connection.receive({"t": "cancel", "id": "r1"})
    assert_error("frame.invalid", connection.receive, REQ)


def test_handshake_order():
    for frame in [REQ, {"t": "cancel", "id": "r1"}, WELCOME]:
        assert_error("handshake.invalid", Connection().receive, frame)
    for frame in [HELLO, REQ, {"t": "cancel", "id": "r1"}]:
        connection = Connection()
        connection.receive(HELLO)
        assert_error("handshake.invalid", connection.receive, frame)
    assert_error("handshake.invalid", ready().receive, HELLO)


def test_inflight_limit():
    connection = ready()
    for i in range(8):
        connection.admit(connection.receive(REQ | {"id": str(i)}))
    overflow = connection.receive(REQ | {"id": "overflow"})
    assert_error("quota.host", connection.admit, overflow)
    connection.responded(connection.request("overflow"))
    connection.responded(connection.request("0"))
    connection.receive(REQ | {"id": "0"})


scalar_text = st.text(st.characters(blacklist_categories=("Cs",)), max_size=40)
json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-MAX_INT, MAX_INT) | scalar_text,
    lambda children: st.lists(children, max_size=5) | st.dictionaries(scalar_text, children, max_size=5),
    max_leaves=30,
)


@given(st.dictionaries(scalar_text, json_values, max_size=5), st.lists(st.integers(1, 40), min_size=1, max_size=20))
def test_round_trip(frame, chunks):
    data = encode(frame) + encode(frame)
    decoder = Decoder()
    result = []
    offset = 0
    for size in chunks:
        result.extend(decoder.feed(data[offset : offset + size]))
        offset += size
    result.extend(decoder.feed(data[offset:]))
    assert result == [frame, frame]
    decoder.finish()


@given(st.from_regex(r"[A-Za-z0-9_-]{1,32}", fullmatch=True), st.dictionaries(scalar_text, json_values, max_size=5))
def test_request_round_trip(request_id, args):
    frame = REQ | {"id": request_id, "args": args}
    decoded = decode(encode(frame)[4:])
    validate_envelope(decoded, sender="client")
    connection = ready()
    connection.receive(decoded)
    connection.responded(connection.request(request_id))
    assert not connection.inflight


@pytest.mark.parametrize(
    "patch",
    [
        {"code": "bad-code"},
        {"message": ""},
        {"message": "x" * 401},
        {"retryable": 1},
        {"hint": None},
        {"hint": ""},
        {"fix": {"argv": []}},
        {"fix": {"argv": [""] * 33}},
        {"retry_after_ms": 0},
        {"retry_after_ms": 3600001},
    ],
)
def test_error_body_bounds(patch):
    assert_error("frame.invalid", validate_envelope, {"t": "fatal", "error": ERROR | patch})


def test_welcome_nested_shapes():
    frame = WELCOME | {
        "workspace": {"id": "0" * 32, "prefix": "ORCH"},
        "peer": {"uid": 1000, "class": "person", "person": "p_" + "a" * 32},
    }
    validate_envelope(frame)
    for patch in [
        {"host_uid": 2**32},
        {"host_uid": True},
        {"tier": "C"},
        {"workspace": {"id": "bad", "prefix": "ORCH"}},
        {"peer": frame["peer"] | {"person": "p_sev"}},
        {"peer": {"uid": 1000, "class": "person"}},
        {"limits": {"frame": MAX_FRAME}},
        {"registry": "sha256:bad"},
        {"ops": {"bad-op": 1}},
        {"caps": [""] * 65},
    ]:
        assert_error("frame.invalid", validate_envelope, frame | patch)


def test_map_and_string_boundaries():
    names = ["op" + "a" * n for n in range(257)]
    validate_envelope(HELLO | {"ops": dict.fromkeys(names[:256], 1000), "client": "😀" * 64})
    assert_error("handshake.invalid", validate_envelope, HELLO | {"ops": dict.fromkeys(names, 1)})
    files = {"a" * n: {"b64": ""} for n in range(1, 18)}
    assert_error("req.invalid", validate_envelope, REQ | {"files": files})
    del files["a" * 17]
    validate_envelope(REQ | {"files": files, "auth": {"grant": "x" * 128, "session": "x" * 64}})
    assert_error("req.invalid", validate_envelope, REQ | {"auth": {"grant": "x" * 129}})
    assert_error("req.invalid", validate_envelope, REQ | {"auth": {"session": "x" * 65}})


@pytest.mark.parametrize(
    "code,exit_code,retryable,fate",
    [
        ("frame.invalid", 6, False, "close"),
        ("frame.too_large", 6, False, "close"),
        ("handshake.invalid", 6, False, "close"),
        ("peer.denied", 3, False, "close"),
        ("host.version_mismatch", 1, False, "close"),
        ("req.invalid", 6, False, "stays"),
        ("quota.host", 9, True, "stays"),
        ("conn.overloaded", 9, True, "close"),
        ("result.too_large", 1, False, "stays"),
        ("internal", 1, False, "stays"),
        ("cancelled", 9, True, "stays"),
        ("outcome.unknown", 9, True, "stays"),
    ],
)
def test_error_dispositions(code, exit_code, retryable, fate):
    error = FrameError(code, "Protocol error", "r1" if fate == "stays" else None)
    assert (error.exit, error.retryable, error.fate) == (exit_code, retryable, fate)
    validate_envelope(error.response(), sender="host")


@pytest.mark.parametrize("request_id", ["", "unknown id", "x" * 33])
def test_arbitrary_cancel_ids_are_ignored(request_id):
    connection = ready()
    frame = {"t": "cancel", "id": request_id}
    validate_envelope(decode(encode(frame)[4:]), sender="client")
    assert connection.receive(frame) is None
    assert connection.phase == "ready" and not connection.inflight


def test_cancelled_handler_keeps_slot_and_stale_completion_cannot_release_reused_id():
    connection = ready()
    old = connection.receive(REQ | {"id": "a"})
    connection.admit(old)
    connection.start(old, "read")
    assert connection.cancel("a") == "cancelled"
    assert connection.responded(old)
    replacement = connection.receive(REQ | {"id": "a"})
    connection.admit(replacement)
    connection.start(replacement, "read")
    assert not connection.responded(old)
    assert connection.request("a") is replacement
    for i in range(6):
        request = connection.receive(REQ | {"id": str(i)})
        connection.admit(request)
        connection.start(request, "read")
        connection.cancel(request.id)
        connection.responded(request)
    assert connection.slots == 8
    overflow = connection.receive(REQ | {"id": "overflow"})
    assert_error("quota.host", connection.admit, overflow)
    connection.responded(overflow)
    connection.stopped(old)
    assert connection.slots == 7
    connection.stopped(old)
    assert not connection.responded(old)
    assert connection.request("a") is replacement
    connection.admit(connection.receive(REQ | {"id": "next"}))
    assert connection.slots == 8


@pytest.mark.parametrize("error", ["unknown_command", "op.unsupported", "host.restarting", "op.not_allowed"])
def test_dispatch_checks_precede_quota(error):
    connection = ready()
    for i in range(8):
        connection.admit(connection.receive(REQ | {"id": str(i)}))
    # A ninth reserved ID also awaits its response.
    ninth = connection.receive(REQ | {"id": "ninth"})
    assert_error("quota.host", connection.admit, ninth)
    frame = REQ | {"id": "check", "op": "unknown"}
    request = connection.receive(frame)

    def dispatch():
        # H1c owns these checks and calls admit only if they pass.
        if frame["op"] == "unknown":
            return error
        connection.admit(request)

    assert dispatch() == error
    assert connection.slots == 8
    assert connection.request("check") is request
    assert connection.responded(request)


def test_start_requires_known_admitted_handle():
    connection = ready()
    with pytest.raises(ValueError):
        connection.start("unknown", "read")
    request = connection.receive(REQ)
    with pytest.raises(ValueError):
        connection.start(request, "read")
    connection.admit(request)
    assert connection.start(request, "read")
    connection.stopped(request)
    assert connection.slots == 1
    connection.responded(request)
    assert connection.slots == 0


def test_connection_quota_is_fatal():
    error = FrameError("quota.host")
    assert (error.exit, error.retryable, error.fate) == (9, True, "close")
    frame = error.response()
    assert frame["t"] == "fatal" and "id" not in frame
    validate_envelope(frame, sender="host")
    request_error = FrameError("quota.host", request_id="r1")
    assert request_error.fate == "stays"
    assert request_error.response()["id"] == "r1"
    validate_envelope(request_error.response(), sender="host")


@pytest.mark.parametrize("code", ["req.invalid", "quota.host", "cancelled", "outcome.unknown"])
@pytest.mark.parametrize("request_id", [None, "", "unknown id", "x" * 33, 1])
def test_request_errors_require_valid_ids(code, request_id):
    if code == "quota.host" and request_id is None:
        return
    with pytest.raises(ValueError, match="valid request id"):
        FrameError(code, request_id=request_id)


def test_error_default_messages_are_specific():
    codes = [
        "frame.invalid",
        "frame.too_large",
        "handshake.invalid",
        "peer.denied",
        "host.version_mismatch",
        "req.invalid",
        "quota.host",
        "cancelled",
        "outcome.unknown",
    ]
    messages = [str(FrameError(code, request_id="r1")) for code in codes]
    assert len(set(messages)) == len(codes)
    assert all(messages)


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


@pytest.mark.parametrize("prefix_bytes", [1, 2, 3, 4])
def test_deadline_without_body_bytes(prefix_bytes):
    clock = Clock()
    decoder = Decoder(clock=clock)
    clock.now = 100
    decoder.check_deadline()  # An idle connection has no frame deadline.
    assert decoder.feed(encode(REQ)[:prefix_bytes]) == []
    assert decoder.pending and decoder.body_number == 1
    assert decoder.body_received == 0
    clock.now = 109
    decoder.check_deadline()
    clock.now = 110
    assert_error("frame.invalid", decoder.check_deadline)
    assert decoder.failed and decoder.feed(encode(REQ)) == []


def test_trickled_prefix_and_body_do_not_extend_deadline():
    clock = Clock()
    decoder = Decoder(clock=clock)
    data = encode(REQ)
    assert decoder.feed(data[:1]) == []
    for i in range(1, 10):
        clock.now = i
        assert decoder.feed(data[i : i + 1]) == []
    clock.now = 10
    frames = decoder.feed(data[10:])
    assert len(frames) == 1 and frames[0].code == "frame.invalid"
    assert decoder.failed


def test_deadline_rolls_over_at_first_byte_of_next_frame():
    clock = Clock()
    decoder = Decoder(clock=clock)
    data = encode(REQ)
    decoder.feed(data[:1])
    clock.now = 9
    assert decoder.feed(data[1:] + data[:1]) == [REQ]
    assert decoder.pending and not decoder.body_started and decoder.body_number == 2
    clock.now = 10
    decoder.check_deadline()
    clock.now = 18
    assert decoder.feed(data[1:]) == [REQ]
    assert not decoder.pending
    clock.now = 100
    decoder.check_deadline()
    assert decoder.feed(data[:1]) == []
    clock.now = 110
    assert_error("frame.invalid", decoder.check_deadline)


def test_feed_is_eager_even_when_result_is_not_consumed():
    decoder = Decoder()
    result = decoder.feed(encode(REQ)[:2])
    assert isinstance(result, list) and result == []
    assert decoder.pending
    result = decoder.feed(encode(REQ)[2:] + wire(b"[]") + encode(REQ))
    assert isinstance(result, list) and len(result) == 2
    assert result[0] == REQ and result[1].code == "frame.invalid"
    assert decoder.failed


@pytest.mark.parametrize("value", [{}, [], "", 0, True, None])
def test_json_value_cap(value):
    # The root object and its array count; member names do not.
    frame = {"values": [value] * (MAX_VALUES - 2)}
    assert decode(encode(frame)[4:]) == frame
    frame["values"].append(value)
    assert_error("frame.invalid", decode, json.dumps(frame).encode())
    assert_error("frame.invalid", encode, frame)
    assert_error("frame.invalid", validate_envelope, frame)


def test_value_budget_is_shared_across_nested_containers():
    frame = {"a": [None] * (MAX_VALUES // 2), "b": [None] * (MAX_VALUES // 2)}
    assert_error("frame.invalid", decode, json.dumps(frame).encode())


def test_two_mib_of_empty_objects_is_fatal():
    count = (MAX_FRAME - 7) // 3
    body = b'{"x":[' + b"{}," * (count - 1) + b"{}]}"
    body += b" " * (MAX_FRAME - len(body))
    assert len(body) == MAX_FRAME
    assert_error("frame.invalid", decode, body)
    decoder = Decoder()
    frames = decoder.feed(wire(body) + encode(REQ))
    assert len(frames) == 1 and frames[0].code == "frame.invalid"
    assert frames[0].fate == "close" and decoder.failed
