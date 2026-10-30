"""#1285: replay contracts/hub/vectors_hooks.json against orch_hub.contract.hooks and the link op table."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from orch_hub.contract import hooks as HK
from orch_hub.contract.hub import LINK_OPS, LINK_OPTIONAL

DOC = json.loads((Path(__file__).parents[3] / "contracts" / "hub" / "vectors_hooks.json").read_text())
KINDS = {"token_hash", "request", "reply", "github", "generic", "basic"}


def _table_ok(op: str, body: dict) -> bool:
    _d, _m, spec = LINK_OPS[op]
    opt = LINK_OPTIONAL.get(op, frozenset())
    return (
        type(body) is dict
        and set(spec) - opt <= set(body) <= set(spec)
        and all(ok(body[k]) for k, ok in spec.items() if k in body)
    )


def _mac_ok(mac, header_hex: str | None, body: bytes) -> bool:
    import hmac

    mac.update(body)
    return header_hex is not None and hmac.compare_digest(mac.hexdigest(), header_hex)


def run(c: dict):
    k = c["check"]
    if k == "token_hash":
        try:
            return HK.token_hash(c["token"])
        except ValueError:
            return None
    if k == "request":
        # the op table (per-field) and the whole-body check agree; replaces != token_hash is the whole-body rule
        whole = HK.put_ok(c["body"]) if c["op"] == "hook_put" else True
        return _table_ok(c["op"], c["body"]) and whole
    if k == "reply":
        f = {"hook_put": HK.put_reply_ok, "hook_pull": HK.pull_reply_ok, "hook_ack": HK.ack_reply_ok}[c["op"]]
        return f(c["body"])
    body = bytes.fromhex(c.get("body_hex", ""))
    if k == "github":
        h = HK.parse_github(c["header"])
        return _mac_ok(HK.github_mac(c["secret"]), h, body)
    if k == "generic":
        p = HK.parse_generic(c["header"])
        return (
            p is not None
            and HK.generic_fresh(p[0], c["now_s"])
            and _mac_ok(HK.generic_mac(c["secret"], p[0]), p[1], body)
        )
    if k == "basic":
        return HK.basic_ok(c["header"], c["secret"])
    raise AssertionError(f"unhandled check kind {k}")


def test_every_kind_is_handled_and_cases_exist():
    assert {c["check"] for c in DOC["cases"]} == KINDS
    assert len(DOC["cases"]) >= 60


def test_constants_match():
    k = DOC["constants"]
    assert k["sources"] == list(HK.SOURCES) and k["kinds"] == list(HK.KINDS) and k["basic_user"] == HK.BASIC_USER
    assert (k["generic_window_s"], k["body_cap"], k["max_hooks"], k["pull_max"]) == (
        HK.GENERIC_WINDOW_S,
        HK.BODY_CAP,
        HK.MAX_HOOKS_PER_WORKSPACE,
        HK.PULL_MAX,
    )


@pytest.mark.parametrize("c", DOC["cases"], ids=lambda c: c["name"])
def test_case(c):
    assert run(c) == c["expect"]


def test_vector_file_is_current(tmp_path):
    import subprocess
    import sys

    gen = Path(__file__).parents[3] / "contracts" / "hub" / "make_vectors_hooks.py"
    before = (gen.parent / "vectors_hooks.json").read_text()
    subprocess.run([sys.executable, str(gen)], check=True, capture_output=True)
    assert (gen.parent / "vectors_hooks.json").read_text() == before
