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
HASH = "Ab" + TOKEN + "xyA"  # 43 characters: base64url of 32 bytes, as a random hash or ticket reads
R = "[redacted]"
SIG = "QQ" + TOKEN + "Bc" * 22 + "BA"  # 86 characters: base64url of 64 bytes

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


# --- answers the client reuses: their exact-shape key fields are returned as they are (review F1, F2) ---------------

CARD = {
    "o": {"hk_root_pub": KEY, "hxk_pub": KEY, "tls_spki_sha256": HASH, "relay_url": None, "note": f"x {TOKEN}"},
    "op_cert": {"o": {"hk_op_pub": KEY}, "sig": SIG},
    "sig": SIG,
}


def test_the_hub_link_answer_card_comes_back_byte_for_byte(served, monkeypatch):
    """The CLI and the dashboard send the card of ``hub.link`` back in ``hub.connect``: a redacted key would make every
    join by link to that hub fail. Only the exact-shape key fields are kept; any other text is still redacted."""
    from orch.hubclient import connectlink

    got = {"hub_id": "h", "hub_card": CARD, "code": "c", "endpoint": "e", "name": f"n {TOKEN}", "dev": False}
    monkeypatch.setattr(connectlink, "resolve", lambda text, now_ms, dev_mode: got)
    res = ok(served.client().req("hub.link", {"link": "orch://hub/whatever-link"}))["data"]
    want = {**CARD, "o": {**CARD["o"], "note": "x [redacted]"}}
    assert res["hub_card"] == want and res["name"] == "n [redacted]"


@pytest.mark.parametrize(
    ("op", "first", "second", "fn", "answer", "want"),
    [
        ("space.get", "spaceops", "spaceops", "execute", {"data": KEY, "name": TOKEN}, {"data": KEY, "name": R}),
        ("term.attach", "terms", "terms", "complete", {"ticket": HASH, "note": TOKEN}, {"ticket": HASH, "note": R}),
        (
            "publish.send", "publishing", "publishing", "execute",
            {"link": f"https://p.example/s/{TOKEN}#t={HASH}"}, {"link": f"https://p.example/s/[redacted]#t={HASH}"},
        ),
    ],
)  # fmt: skip
def test_a_reused_random_field_of_a_second_phase_answer_is_kept(
    served, monkeypatch, op, first, second, fn, answer, want
):
    async def run(call, planned):
        return ok_result(answer)

    _patch(monkeypatch, first, second, fn, {"plan": {}}, run)
    assert ok(served.client().req(op, {}, idem=_idem(op)))["data"] == want


def test_a_kept_path_with_free_text_or_a_secret_is_still_redacted():
    from orch.host.auth import Redactor

    r = Redactor("gr_0" + "A" * 25 + "." + "B" * 43, "hub.link")
    assert r.obj({"data": {"hub_card": {"o": {"hxk_pub": f"free text {TOKEN}"}}}}) == {
        "data": {"hub_card": {"o": {"hxk_pub": "free text [redacted]"}}}
    }
    grant_shaped = "gr_0" + "A" * 25 + "." + "B" * 43
    assert r.obj({"data": {"data": grant_shaped}}) == {"data": {"data": "[redacted]"}}  # not a kept op's path
    assert Redactor(None, "space.get").obj({"data": {"data": "gr_0" + "A" * 25 + "." + "B" * 43}}) == {
        "data": {"data": "[redacted]"}  # the grant shape and the presented secret are never kept
    }


def test_the_held_card_and_the_share_plan_digest_are_kept_at_their_paths_only():
    from orch.host.auth import Redactor

    card = Redactor(None, "hub.status").obj({"data": {"hub_card": CARD, "other": KEY}})["data"]
    assert card["hub_card"]["o"]["hxk_pub"] == KEY and card["hub_card"]["sig"] == SIG and card["other"] != KEY
    plan = Redactor(None, "share.preview").obj({"data": {"plan": HASH, "items": [HASH]}})["data"]
    assert plan == {"plan": HASH, "items": [Redactor().text(HASH)]} and "[redacted]" in plan["items"][0]


# --- one table (review F3): every op outside SECOND_PHASE is answered by its worker phase, redacted -------------------

_ROUTES = (
    "reads", "records", "workdirs", "readerops", "phonehints", "terms", "agents", "runnergrants", "runners",
    "transcripts", "worktrees", "publishing", "hubops", "cosmeticops", "shareops", "spaceops", "orgops", "marketops",
    "peerops",
)  # fmt: skip


def test_every_op_outside_the_table_has_its_worker_answer_redacted(served, monkeypatch):
    """Whatever route an op takes on the worker, an answer that looks like a plan is redacted unless the op is in
    ``SECOND_PHASE``: a two-phase op cannot be added anywhere but in the one table."""
    from orch.host.human import HumanOps

    answer = ok_result({"plan": {"x": TOKEN}})
    for name in _ROUTES:
        monkeypatch.setattr(importlib.import_module(f"orch.host.{name}"), "serve", lambda call: answer)
    for meth in ("_host_op", "_file_op", "_registry"):
        monkeypatch.setattr(dispatch.RegistryBackend, meth, lambda self, call, *a: answer)
    monkeypatch.setattr(HumanOps, "serve", lambda self, call, redactor: answer)
    reached, refused, client = [], {}, served.client()
    # sub.close: after the (redacted) worker answer, run() closes the subscription named in args, which {} lacks
    for op in sorted(set(dispatch.served_ops()) - dispatch.TWO_PHASE_OPS - dispatch.WAIT_OPS - {"sub.close"}):
        res = client.req(op, {}, idem=_idem(op))
        if res.get("ok"):
            assert res["result"]["data"] == {"plan": {"x": "[redacted]"}}, op
            reached.append(op)
        else:
            assert TOKEN not in str(res), op
            refused[op] = res["error"]["code"]
    assert len(reached) >= 80, refused  # most ops reach the worker as the owner
    assert set(refused.values()) <= {"human_only", "op.not_allowed", "grant.required"}, refused  # refused before it
