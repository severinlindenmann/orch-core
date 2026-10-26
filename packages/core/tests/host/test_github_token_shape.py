"""Shared shapes keep unregistered GitHub credentials out of requests and replies."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from orch.host.auth import REDACTED, Redactor, secret_in_request
from orch.host.errors import HostError
from orch.tokenshape import GITHUB_TOKEN_BYTES, GITHUB_TOKEN_SHAPE

TOKENS = ["github_pat_" + "A" * 40] + ["gh" + kind + "_" + "b" * 36 for kind in "pousr"]


@pytest.mark.parametrize("token", TOKENS)
def test_shapes_match_tokens(token):
    assert GITHUB_TOKEN_SHAPE.fullmatch(token)
    assert GITHUB_TOKEN_BYTES.fullmatch(token.encode())


@pytest.mark.parametrize("prefix", ["github_pat_"] + ["gh" + kind + "_" for kind in "pousr"])
def test_minimum_tail_length(prefix):
    short = prefix + "A" * 19
    assert not GITHUB_TOKEN_SHAPE.search(short)
    assert not GITHUB_TOKEN_BYTES.search(short.encode())
    assert not secret_in_request({"args": {"text": short}})
    assert Redactor().text(short) == short
    assert GITHUB_TOKEN_SHAPE.fullmatch(prefix + "A" * 20)
    assert GITHUB_TOKEN_BYTES.fullmatch((prefix + "A" * 20).encode())


def test_missing_prefix_separator_is_not_a_token():
    text = "github_pat" + "A" * 40
    assert not GITHUB_TOKEN_SHAPE.search(text)
    assert not GITHUB_TOKEN_BYTES.search(text.encode())


@pytest.mark.parametrize("token", TOKENS)
def test_request_refuses_tokens_in_every_input_location(token):
    encoded = base64.b64encode(b"\xff prefix " + token.encode() + b" suffix").decode()
    frames = [
        {"args": {"text": token}},
        {"args": {token: "value"}},
        {"opts": {"nested": [token]}},
        {"opts": {token: True}},
        {"files": {token: {"b64": ""}}},
        {"files": {"note": {"name": token}}},
        {"files": {"note": {"b64": encoded}}},
        {"op": "blob.put", "args": {"b64": encoded}},
    ]
    for frame in frames:
        assert secret_in_request(frame)


def test_ordinary_request_passes():
    assert not secret_in_request(
        {
            "op": "ticket.new",
            "args": {"title": "ordinary"},
            "opts": {"quiet": True},
            "files": {"note": {"b64": base64.b64encode(b"ordinary text").decode()}},
        }
    )


@pytest.mark.parametrize("token", TOKENS)
def test_redactor_scrubs_text_nested_keys_values_and_errors(token):
    redactor = Redactor()
    assert redactor.text("before " + token + " after") == "before " + REDACTED + " after"
    assert redactor.obj({token: [{"nested": (token,)}]}) == {REDACTED: [{"nested": [REDACTED]}]}
    error = redactor.error(HostError("invalid.input", token, hint=token, fix=["orch", token]))
    assert error.message == error.hint == REDACTED
    assert error.fix == ["orch", REDACTED]


def test_token_pattern_has_one_source():
    root = Path(__file__).resolve().parents[2] / "src" / "orch"
    duplicates = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path != root / "tokenshape.py" and GITHUB_TOKEN_SHAPE.pattern in path.read_text()
    ]
    assert duplicates == []
