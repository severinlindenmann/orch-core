"""H13-0, fail closed: every public function of ``orch.crypto.push``, ``drop`` and ``recovery`` raises only its
module's error type (``PushError``, ``DropError``/``DropTagError``, ``RecoveryError``/``RecoveryTagError``) on hostile
input, never a raw ``KeyError``, ``TypeError``, ``ValueError``, ``IndexError``, ``RecursionError`` or ``InvalidTag``,
and never returns partial data. The error types are not ``ValueError`` subclasses.

One class per malformation: wrong types, unknown and missing fields, bad base64 and lengths, truncation, oversize,
deep nesting, a wrong label or epoch, and byte-level truncation and bit flips of a valid frame."""

from __future__ import annotations

import types

import pytest

from orch import canon
from orch.crypto import drop, push, recovery

from .vectors import S2, priv, pub

D = S2["drop"]
DESC = D["descriptor"]["o"]
W = D["wraps"]["device"]
VALID_FRAME = bytes.fromhex(next(s for s in D["upload_frame"]["cases"] if s["name"] == "valid_frame")["body_hex"])
WS = bytes.fromhex(S2["ids"]["workspace_a"])
PHONE = bytes.fromhex(S2["ids"]["device_phone"])

JUNK = [
    None, True, 0, -1, 2**64, 1.5, "", "x", "ZZ", b"", b"\x00" * 3, [], [1, "a"], [[1]], [{}], {}, {"x": 1},
    object(), "A" * 5000,
]  # fmt: skip


def test_error_types_are_closed_and_not_value_errors():
    for e in (drop.DropError, push.PushError, recovery.RecoveryError):
        assert not issubclass(e, (ValueError, TypeError, KeyError, LookupError))
    assert issubclass(drop.DropTagError, drop.DropError)
    assert issubclass(recovery.RecoveryTagError, recovery.RecoveryError)


@pytest.mark.parametrize("mod", [drop, push, recovery], ids=lambda m: m.__name__)
def test_every_public_function_is_a_closed_boundary(mod):
    for name in mod.__all__:
        obj = getattr(mod, name)
        if isinstance(obj, types.FunctionType):
            assert getattr(obj, "__orch_closed__", False), name


def _closed(err, fn, *args, **kw):
    """The call either succeeds (some junk is a valid value of that field) or raises ``err``; never anything else."""
    try:
        fn(*args, **kw)
    except err:
        return False
    except Exception as e:  # noqa: BLE001 - the point of the test
        raise AssertionError(f"{fn.__name__}{args!r:.80}: raw {type(e).__name__}: {e}") from None
    return True


def _only(err, fn, *args, **kw):
    """The call raises ``err`` (it is refused, and refused with the module's error)."""
    assert not _closed(err, fn, *args, **kw), f"{fn.__name__} accepted {args!r:.80}"


@pytest.mark.parametrize("field", sorted(drop.DESCRIPTOR_FIELDS))
def test_descriptor_every_field_with_every_junk_value(field):
    for v in JUNK:
        if DESC[field] == v and type(DESC[field]) is type(v):
            continue
        _closed(drop.DropError, drop.check_drop_object, {**DESC, field: v})
        _only(
            drop.DropError,
            drop.verify_drop_object,
            {"o": {**DESC, field: v}, "sig": D["descriptor"]["sig"]},
            pub("phone.sig"),
        )
    _only(drop.DropError, drop.check_drop_object, {k: v for k, v in DESC.items() if k != field})  # missing


def test_descriptor_recipients_hostile_lists():
    """The scan's finding: a mixed or unhashable ``recipients`` list escaped as a raw ``TypeError``."""
    for r in ([1, "a"], [[1]], [{}], [None], ["A" * 32], [DESC["space_id"], 5], [DESC["space_id"]] * 2):
        _only(drop.DropError, drop.check_drop_object, {**DESC, "recipients": r})


@pytest.mark.parametrize("field", sorted(drop.WRAP_FIELDS))
def test_wrap_every_field_with_every_junk_value(field):
    for v in JUNK:
        if W[field] == v and type(W[field]) is type(v):
            continue
        rec = {**W, field: v}
        _closed(drop.DropError, drop.check_wrap_record, rec)
        _only(drop.DropError, drop.open_wrap, rec, DESC, PHONE, kx_priv=priv("phone.kx"))
    _only(drop.DropError, drop.check_wrap_record, {k: v for k, v in W.items() if k != field})


def test_openers_with_junk_arguments():
    for v in JUNK:
        _only(drop.DropError, drop.parse_frame, v)
        _only(drop.DropError, drop.decrypt_blob, bytes.fromhex(D["dek"]), bytes.fromhex(D["object_id"]), 1, v)
        _only(drop.DropError, drop.decrypt_blob, v, bytes.fromhex(D["object_id"]), 1, bytes.fromhex(D["blob"]))
        _only(drop.DropError, drop.open_meta, bytes.fromhex(D["dek"]), bytes.fromhex(D["object_id"]), v, b"x" * 40)
        _only(drop.DropError, drop.open_wrap, v, DESC, WS, wk=bytes(32))
        _only(drop.DropError, drop.open_wrap, W, v, PHONE, kx_priv=priv("phone.kx"))
        _only(drop.DropError, drop.open_wrap, W, DESC, v, kx_priv=priv("phone.kx"))
        _only(drop.DropError, drop.open_wrap, W, DESC, PHONE, kx_priv=v)
        _only(drop.DropError, drop.verify_drop_object, v, pub("phone.sig"))
        _only(drop.DropError, drop.verify_drop_object, D["descriptor"], v)
        _only(drop.DropError, drop.drop_claim_signed_bytes, v)
        _only(drop.DropError, drop.parse_blob_header, v, bytes(16), 1)
        _only(drop.DropError, drop.decrypt_chunk, bytes(32), bytes(16), 1, bytes(40), 0, v, True)
        _only(drop.DropError, drop.reframe, VALID_FRAME, v)
        _only(push.PushError, push.open_push, v if isinstance(v, bytes) else canon.dumps({"x": 1}), {}, 0)
        _only(push.PushError, push.open_push, canon.dumps({"v": 2, "ws": WS.hex(), "epoch": 1, "c": "AAAA"}), v, 0)
        _closed(push.PushError, push.pad_label, v)
        _only(recovery.RecoveryError, recovery.open_recovery, v, bytes(32), WS, bytes(16), 1)
        if not isinstance(v, bytes):
            _only(recovery.RecoveryError, recovery.RecoveryEnvelope.from_host_bytes, v)
    _only(drop.DropError, drop.open_wrap, W, DESC, PHONE, kx_priv="not a key")  # a wrong key type is closed too


def test_frame_truncations_and_bit_flips_never_return_partial_data():
    for cut in range(0, len(VALID_FRAME), 7):
        _only(drop.DropError, drop.parse_frame, VALID_FRAME[:cut])
    for i in range(0, len(VALID_FRAME), 3):
        b = bytearray(VALID_FRAME)
        b[i] ^= 0x01
        try:
            head, blob = drop.parse_frame(bytes(b))
        except drop.DropError:
            continue
        # anything accepted is complete: it re-encodes to exactly the input (a flip inside the unsigned-at-parse
        # signature text, for example, still parses; the reader verifies the signature next)
        assert drop.encode_frame(head["descriptor"], head["wraps"], blob) == bytes(b)


def test_deep_and_oversized_heads():
    deep = b"[" * 5000 + b"]" * 5000
    _only(drop.DropError, drop.parse_frame, b"ODU2" + len(deep).to_bytes(4, "big") + deep)
    _only(drop.DropError, drop.parse_frame, b"ODU2" + (drop.MAX_HEAD + 1).to_bytes(4, "big"))
    _only(push.PushError, push.open_push, b"[" * 3000, {}, 0)
    _only(recovery.RecoveryError, recovery.RecoveryEnvelope.from_host_bytes, b"[" * 500 + b"]" * 12)


def test_builders_with_junk_inputs():
    dek, obj = bytes.fromhex(D["dek"]), bytes.fromhex(D["object_id"])
    for v in JUNK:
        _only(drop.DropError, drop.seal_version, obj, 1, b"x", {"x": 1.5, "v": v})
        _only(drop.DropError, drop.seal_version, v, 1, b"x", {})
        _only(drop.DropError, drop.drop_object, v, space_id=WS, object_kind="file", parent_hash=None,
              author_kind="workspace", author_id=WS, recipients=[WS.hex()], origin="human", created_ms=0,
              expires_ms=1)  # fmt: skip
        _only(drop.DropError, drop.wrap_wk, v, WS, 1, obj, 1, dek)
        _only(drop.DropError, drop.wrap_sealed, "device", v, bytes(16), 0, obj, 1, dek)
        _only(drop.DropError, drop.rewrap_same_dek, DESC, v, bytes(32), 1)
        _only(drop.DropError, drop.drop_claim_object, obj, WS, v)
        if not isinstance(v, str) or not v:
            _only(push.PushError, push.push_payload, "question", "q-1", v, 0)
        _only(push.PushError, push.k_push, v, WS, 1)
        _only(recovery.RecoveryError, recovery.seal_recovery, v, WS, 1, obj, 1, dek)
    _only(drop.DropError, drop.drop_object, space_id=obj)  # missing arguments: closed as well


class Boom(Exception):
    pass


@pytest.mark.parametrize("exc", [NameError, AttributeError, AssertionError, MemoryError, RecursionError])
def test_bugs_and_resource_errors_propagate(exc):
    """Review F5: a bug or a resource problem is not malformed input; it is never turned into the module's error."""
    from orch.crypto._closed import closed

    def f():
        raise exc("x")

    with pytest.raises(exc):
        closed(drop.DropError, drop.DropError, drop.DropTagError)(f)()


def test_the_cause_is_chained_but_not_in_the_message():
    from orch.crypto._closed import closed

    def f():
        raise KeyError("secret-detail")

    with pytest.raises(drop.DropError) as e:
        closed(drop.DropError, drop.DropError, drop.DropTagError)(f)()
    assert "secret-detail" not in str(e.value)
    assert isinstance(e.value.__cause__, KeyError)
