"""#1285: replay contracts/hub/vectors_hooks.json with a hand-written port (no orch_hub import)."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
from pathlib import Path

import pytest

from orch.hubclient.link import W2H_OPS as LINK_OPS
from orch.hubclient.tlslink.client import _REPLY
from orch.hubclient.tlslink.client import W2H_OPS as TLS_OPS

DOC = json.loads((Path(__file__).parents[4] / "contracts" / "hub" / "vectors_hooks.json").read_text())
KINDS = {"token_hash", "request", "reply", "github", "generic", "basic"}
SRC = ("github", "generic", "databricks")


def _hash(x):
    return type(x) is str and re.fullmatch(r"[0-9a-f]{64}", x) is not None


def request_ok(op, b):
    if type(b) is not dict:
        return False
    if op == "hook_put":
        need = {"token_hash", "followup", "source", "secret", "expires_ms"}
        if not need <= set(b) <= need | {"replaces"}:
            return False
        return (
            _hash(b["token_hash"])
            and type(b["followup"]) is str
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", b["followup"]) is not None
            and b["source"] in SRC
            and type(b["secret"]) is str
            and re.fullmatch(r"hs_[A-Za-z0-9_-]{43}", b["secret"]) is not None
            and type(b["expires_ms"]) is int
            and b["expires_ms"] >= 1
            and ("replaces" not in b or (_hash(b["replaces"]) and b["replaces"] != b["token_hash"]))
        )
    if op == "hook_revoke":
        return set(b) == {"token_hash"} and _hash(b["token_hash"])
    if op == "hook_pull":
        return set(b) == {"since"} and type(b["since"]) is int and b["since"] >= 0
    if op == "hook_ack":
        return set(b) == {"seq"} and type(b["seq"]) is int and b["seq"] >= 1
    raise AssertionError(op)


def reply_ok(op, b):
    if not _REPLY[op](b):
        return False
    if op == "hook_pull":  # strictly increasing seq (the vectors fix it)
        seqs = [r["seq"] for r in b["rings"]]
        return seqs == sorted(set(seqs))
    return True


def _mac(secret, prefix, body):
    m = hmac.new(secret.encode(), digestmod=hashlib.sha256)
    m.update(prefix + body)
    return m.hexdigest()


def run(c):
    k = c["check"]
    if k == "token_hash":
        t = c["token"]
        if re.fullmatch(r"hh_[A-Za-z0-9_-]{43}", t) is None:
            return None
        return hashlib.sha256(t.encode()).hexdigest()
    if k == "request":
        return request_ok(c["op"], c["body"])
    if k == "reply":
        return reply_ok(c["op"], c["body"])
    body = bytes.fromhex(c.get("body_hex", ""))
    if k == "github":
        m = re.fullmatch(r"sha256=([0-9a-f]{64})", c["header"] or "")
        return bool(m) and hmac.compare_digest(_mac(c["secret"], b"", body), m.group(1))
    if k == "generic":
        m = re.fullmatch(r"t=([0-9]{1,12}),v1=([0-9a-f]{64})", c["header"] or "")
        if not m or abs(c["now_s"] - int(m.group(1))) > 300:
            return False
        return hmac.compare_digest(_mac(c["secret"], m.group(1).encode() + b".", body), m.group(2))
    if k == "basic":
        h = c["header"] or ""
        if not h.startswith("Basic "):
            return False
        try:
            user, sep, pw = base64.b64decode(h[6:], validate=True).partition(b":")
        except (binascii.Error, ValueError):
            return False
        return bool(sep) and hmac.compare_digest(user, b"orch") & hmac.compare_digest(pw, c["secret"].encode())
    raise AssertionError(f"unhandled check kind {k}")


def test_kinds_and_ops_registered():
    assert {c["check"] for c in DOC["cases"]} == KINDS
    for op in ("hook_put", "hook_revoke", "hook_pull", "hook_ack"):
        assert LINK_OPS[op] is None and TLS_OPS[op] is None


@pytest.mark.parametrize("c", DOC["cases"], ids=lambda c: c["name"])
def test_case(c):
    assert run(c) == c["expect"]
