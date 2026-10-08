"""hash_v, hash strings and the gate hash (ticket-format §5). Known answers are computed with hashlib/json here."""

import hashlib
import json

import pytest

from orch.canon import (
    GATE_KEYS_V1,
    HashError,
    check_hash_v,
    format_hash,
    gate_hash,
    hash_jcs,
    parse_hash,
    section_hash,
    sha256_hex,
)

ABC = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def _payload(**over):
    p = {
        "workspace_id": "ws1",
        "uid": "01J9ZP",
        "gate": "requirements",
        "schema": 2,
        "hash_v": 1,
        "sections": {"Context": "café\nline", "Plan": "x"},
        "fields": {"type": "feature", "size": "M", "acceptance": [{"id": "AC1", "text": "t"}]},
        "policy_hash": "sha256:" + "31" * 32,
        "people_hash": "sha256:" + "aa" * 32,
    }
    p.update(over)
    return p


def _ref(p) -> str:
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(p, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    )


def test_sha256_known_answer():
    assert sha256_hex(b"abc") == ABC
    assert sha256_hex(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_format_parse_hash():
    assert format_hash(ABC) == "sha256:" + ABC
    assert parse_hash("sha256:" + ABC) == bytes.fromhex(ABC)
    for bad in [
        "",
        ABC,
        "sha256:" + ABC.upper(),
        "sha256:" + ABC[:-1],
        "sha256:" + ABC + "0",
        "SHA256:" + ABC,
        "sha512:" + ABC,
        "sha256: " + ABC[1:],
    ]:
        with pytest.raises(HashError):
            parse_hash(bad)
    with pytest.raises(HashError):
        format_hash(ABC.upper())
    with pytest.raises(HashError):
        parse_hash(None)  # type: ignore[arg-type]


def test_hash_jcs_known_answer():
    assert (
        hash_jcs({"b": [1, None], "a": "ü"})
        == "sha256:" + hashlib.sha256('{"a":"ü","b":[1,null]}'.encode()).hexdigest()
    )


def test_gate_hash_known_answer_with_normalised_sections():
    p = _payload()
    expected = _ref({**p, "sections": {"Context": "café\nline", "Plan": "x"}})
    assert gate_hash(p) == expected
    # NFD + CRLF in the input hash identically to the normalised text
    denorm = _payload(sections={"Context": "café\r\nline", "Plan": "x"})
    assert gate_hash(denorm) == expected
    assert denorm["sections"]["Context"] == "café\r\nline"  # input not mutated


def test_gate_hash_sensitive_to_every_field():
    base = gate_hash(_payload())
    for k, v in [
        ("gate", "plan"),
        ("uid", "other"),
        ("workspace_id", "ws2"),
        ("schema", 3),
        ("policy_hash", "sha256:" + "00" * 32),
        ("people_hash", "sha256:" + "00" * 32),
        ("fields", {"type": "bug"}),
        ("sections", {"Context": "changed", "Plan": "x"}),
    ]:
        assert gate_hash(_payload(**{k: v})) != base, k
    # trailing whitespace is significant (the format does not strip it)
    assert gate_hash(_payload(sections={"Context": "café\nline ", "Plan": "x"})) != base


@pytest.mark.parametrize("v", [0, 2, -1, "1", 1.0, True, None])
def test_unknown_or_malformed_hash_v_refused(v):
    with pytest.raises(HashError):
        check_hash_v(v)
    with pytest.raises(HashError):
        gate_hash(_payload(hash_v=v))
    with pytest.raises(HashError):
        section_hash("x", v)  # type: ignore[arg-type]


def test_check_hash_v_ok():
    assert check_hash_v(1) == 1


def test_gate_hash_missing_extra_or_bad_inputs():
    p = _payload()
    del p["people_hash"]
    with pytest.raises(HashError):
        gate_hash(p)
    with pytest.raises(HashError):
        gate_hash(_payload(extra=1))
    with pytest.raises(HashError):
        gate_hash(_payload(sections={"a": 1}))
    with pytest.raises(HashError):
        gate_hash(_payload(sections=["x"]))
    with pytest.raises(HashError):
        gate_hash(_payload(fields={"size": 1.5}))  # floats are refused by cj
    with pytest.raises(HashError):
        gate_hash([])  # type: ignore[arg-type]
    assert "hash_v" in GATE_KEYS_V1


def test_section_hash():
    assert section_hash("café\r\n") == "sha256:" + hashlib.sha256("café\n".encode()).hexdigest()
    assert section_hash("café\n") == section_hash("café\n")
    assert section_hash("a") != section_hash("a ")
