"""Redaction is for what LEAVES the host. The worker's answer of a two-phase op is internal (a hub plan with a signed
card, a space item, a launch): the second phase gets it byte for byte, and only the second phase's answer and error
are redacted. Before this, a random base64url key that happened to read ``ghu_...`` was rewritten to ``[redacted]``
inside a hub card between the plan and the join (``hub.refused: bad_signature`` / ``malformed``, about one connect
in 10^4)."""

from __future__ import annotations

import importlib

import pytest

from orch.host import dispatch
from orch.host.errors import HostError
from orch.host.workspace import ok_result
from tests.host import human_helpers
from tests.host.dispatch_helpers import ok
from tests.host.human_helpers import err

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

#: a GitHub token shape inside base64url, as random key material can read
TOKEN = "ghu_B1gu5c9sd_4KWWXuk5Pd7lUGe1lG3UrKHT"
KEY = "BLNQlSn037s5HRV4xz7itvEYFP5EdCGu_Rwrhfg7XOfJUfN" + TOKEN + "-Q"

#: one op per second-phase family: (op, the module serving the worker phase, the second phase's module and function)
FAMILIES = [
    ("hub.refresh", "hubops", "hubops", "execute"),
    ("share.sync", "shareops", "shareops", "execute"),
    ("space.list", "spaceops", "spaceops", "execute"),
    ("org.sync", "orgops", "orgops", "execute"),
    ("peer.send", "peerops", "peerops", "execute"),
    ("market.refresh", "marketops", "marketops", "execute"),
    ("publish.list", "publishing", "publishing", "execute"),
    ("worktree.create", "worktrees", "worktrees", "execute"),
    ("agent.start", "agents", "agents", "complete"),
    ("grant.forget", "runnergrants", "runnergrants", "complete"),
    ("term.open", "terms", "terms", "complete"),
    ("run.transcript", "transcripts", "transcripts", "complete"),
]


def _idem(op: str) -> bool | None:
    return True if dispatch.op_kind(op)[1] == "write" else None  # type: ignore[index]


def _patch(monkeypatch, first: str, second: str, fn: str, worker_answer, second_phase) -> None:
    monkeypatch.setattr(importlib.import_module(f"orch.host.{first}"), "serve", lambda call: worker_answer)
    monkeypatch.setattr(importlib.import_module(f"orch.host.{second}"), fn, second_phase)


@pytest.mark.parametrize(("op", "first", "second", "fn"), FAMILIES)
def test_a_token_shape_in_the_plan_reaches_the_second_phase_and_never_the_client(
    served, monkeypatch, op, first, second, fn
):
    assert op in dispatch.TWO_PHASE_OPS
    seen = {}
    plan = {"plan": {"card": {"o": {"hxk_pub": KEY}, "sig": KEY}, "items": ({"data": KEY},)}}

    async def run(call, planned):
        seen["planned"] = planned
        return ok_result({"echo": f"took {TOKEN}"})

    _patch(monkeypatch, first, second, fn, plan, run)
    res = ok(served.client().req(op, {}, idem=_idem(op)))
    assert seen["planned"] == {"plan": {"card": {"o": {"hxk_pub": KEY}, "sig": KEY}, "items": [{"data": KEY}]}}
    assert res["data"] == {"echo": "took [redacted]"}  # the answer that leaves the host is still redacted


@pytest.mark.parametrize(("op", "first", "second", "fn"), FAMILIES[:2])
def test_a_second_phase_error_is_redacted_too(served, monkeypatch, op, first, second, fn):
    async def run(call, planned):
        raise HostError("hub.refused", f"no: {TOKEN}")

    _patch(monkeypatch, first, second, fn, {"plan": {}}, run)
    res = served.client().req(op, {}, idem=_idem(op))
    assert err(res) == "hub.refused" and TOKEN not in str(res) and "[redacted]" in res["error"]["message"]


def test_a_worker_answer_of_a_one_phase_op_is_still_redacted(served, monkeypatch):
    from orch.host import cosmeticops

    assert "workspace.cosmetic.set" not in dispatch.TWO_PHASE_OPS
    monkeypatch.setattr(cosmeticops, "serve", lambda call: ok_result({"echo": TOKEN}))
    assert ok(served.client().req("workspace.cosmetic.set", {}, idem=True))["data"] == {"echo": "[redacted]"}


def test_plain_copies_views_and_keeps_every_string():
    from types import MappingProxyType

    src = MappingProxyType({"a": (KEY, MappingProxyType({"b": [TOKEN]}))})
    out = dispatch.plain(src)
    assert out == {"a": [KEY, {"b": [TOKEN]}]} and type(out) is dict and type(out["a"][1]) is dict
