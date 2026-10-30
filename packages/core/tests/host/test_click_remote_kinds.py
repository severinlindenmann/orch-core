"""R3 T2 (#1591, design I5): the op kinds a remote-device stream may sign, decided by the click signer alone.

The stream facts (level, person, ``hh``) come from the host (faked: ``stream_helpers.Streams``); the table is
``click/remote_kinds.py``."""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace

import pytest

from orch.click import remote_kinds as K
from orch.click.protocol import CLICK_OPS, SEALED_OPS, ClickError
from orch.click.signer import ClickSigner
from orch.host import review, sequence
from orch.model.emits import CURRENT
from tests.host import human_helpers
from tests.host.stream_helpers import SID, make_world

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture
def world(served):
    w = make_world(served)
    yield w
    w.click.close()


def person_ops() -> list[str]:
    return sorted(o for o in set(CURRENT) | set(review.HOST_PERSON_OPS) | set(CLICK_OPS) if review.person_op_types(o))


# --- the table -------------------------------------------------------------------------------------------------------


def test_op_catalog_walk_every_kind_classified():
    """Every person op, every CLICK_OPS entry, every sealed op and every signer op has a row of the table."""
    for op in {*person_ops(), *CLICK_OPS, *SEALED_OPS}:
        assert op in K.TABLE, f"{op} has no row in click/remote_kinds.py"
    handled = set(re.findall(r'"(\w+)": self\.', inspect.getsource(ClickSigner._dispatch)))  # noqa: SLF001
    for op in handled | {"stepup", "status"}:
        assert op in K.SIGNER_OPS, f"signer op {op} has no row"
    # the closed lists of the design
    assert {o for o, c in K.TABLE.items() if c == K.ALLOWED} == {
        "new", "set", "section.set", "ac.add", "ac.edit", "task.add", "task.edit", "log", "ask", "artifact.add",
        "artifact.replace", "follow_up.cancel", "schedule.cancel", "grant.revoke",
    }  # fmt: skip
    assert {o for o, c in K.TABLE.items() if c == K.PRESENCE} == {
        "approve", "request_changes", "verdict", "answer", "close", "reopen", "move", "ac.accept", "ac.unaccept",
        "task.mark_done", "people.set", "grant", "schedule.add", "follow_up.reschedule", "follow_up.undo",
    }  # fmt: skip
    assert {o for o, c in K.SIGNER_OPS.items() if c == K.ALLOWED} == {
        "prepare", "view", "sign", "cancel", "status", "session_alive", "stash",
    }  # fmt: skip


@pytest.mark.parametrize(
    "op", ["member.add", "policy.set", "remote.off", "hub.future", "addon.new_thing", "frobnicate", 7]
)
def test_unlisted_and_refused_families_are_refused(op):
    assert K.classify(op) == K.REFUSED


def test_a_secret_argument_refuses_any_kind():
    assert K.classify("log", {"sealed": {"x": 1}}) == K.REFUSED
    assert K.classify("log", {"stepup": {}}) == K.REFUSED
    assert K.classify("log", {"args": {"sealed": {}}}) == K.REFUSED
    assert K.classify("log", {"args": {"text": "x"}}) == K.ALLOWED


def test_grant_reveal_or_deposit_refused():
    assert K.classify("grant", {}) == K.PRESENCE  # the agent start (a step of a sequence)
    assert K.classify("grant", {"reveal_key": "AAAA"}) == K.REFUSED
    assert K.classify("grant", {"reveal": True}) == K.REFUSED
    assert K.classify("grant", {"deposit": True}) == K.REFUSED


def test_sequence_strictest_step(monkeypatch):
    quick = {"v": 1, "kind": "quick_task", "title": "t", "description": "d", "checks": [{"text": "c", "cmd": None}],
             "agent": None}  # fmt: skip
    assert K.sequence_class(quick) == K.PRESENCE  # two approvals
    new = {"v": 1, "kind": "new_ticket", "title": "t", "ticket_type": "feature", "set": {}, "sections": {}}
    assert K.sequence_class(new) == K.ALLOWED
    for ops, want in (
        (["new", "log", "approve"], K.PRESENCE),
        (["new", "approve", "member.add"], K.REFUSED),
        (["new", "log", "ask"], K.ALLOWED),
        (["new", "grant", "log"], K.PRESENCE),
        (["new", "addon.grant"], K.REFUSED),
        (["new", "unheard.of"], K.REFUSED),
    ):
        monkeypatch.setattr(sequence, "steps", lambda intent, ops=ops: [{"op": o, "args": {}} for o in ops])
        assert K.sequence_class({}) == want, ops
    monkeypatch.setattr(sequence, "steps", lambda intent: [])
    assert K.sequence_class({}) == K.REFUSED


def test_a_sequence_prepare_is_as_strict_as_its_strictest_step(world, monkeypatch, human):
    _, key = human.ticket()
    seq = {"step": 0, "intent": {"kind": "x"}}
    monkeypatch.setattr(sequence, "steps", lambda i: [{"op": "log", "args": {}}, {"op": "member.add", "args": {}}])
    exc = world.refusal("prepare", op="log", args={"ref": key, "text": "x"}, idem="a_" + "0" * 26, sequence=seq)
    assert exc.code == "remote.kind_refused"


# --- the signer's stream branch -------------------------------------------------------------------------------------


def test_refused_kinds_are_refused_at_prepare_and_at_sign(world, served):
    for op in sorted(o for o, c in K.TABLE.items() if c == K.REFUSED):
        exc = world.refusal("prepare", op=op, args={}, idem="a_" + "0" * 26)
        assert (exc.code, exc.message) == ("remote.kind_refused", "This cannot be done from a remote session."), op
        rid = "hr_" + "1" * 26
        world.signer.approvals[(world.wid, rid)] = SimpleNamespace(op=op, seal=None, deposit=False, reveal=False)
        for follow in ("view", "sign", "cancel"):
            exc = world.refusal(follow, request_id=rid, view_digest="sha256:" + "1" * 64)
            assert exc.code == "remote.kind_refused", (op, follow)
        del world.signer.approvals[(world.wid, rid)]
    assert world.click.signatures == 0


def test_every_signer_op_but_the_listed_ones_is_refused_in_a_stream(world):
    for op in ("custody_on", "custody_off", "rotate", "runner", "github_token", "stepup", "cancel_holds", "nope"):
        exc = world.refusal(op, passphrase="x")
        assert exc.code == "remote.kind_refused", op


def test_an_allowed_kind_is_signed_from_a_stream_with_the_hosts_facts(world, human):
    _, key = human.ticket()
    p = world.prepare("log", {"ref": key, "text": "from a device"})
    digest = world.review(p["request_id"])
    out = world.sign(p["request_id"], digest)
    assert world.click.signatures == 1 and out
    assert set(world.streams.asked) == {SID}


def test_stream_call_uses_host_hh_not_args(world, human):
    """The caller names the stream only: ``hh``, level, person and device in the arguments are refused outright."""
    _, key = human.ticket()
    for field, value in (("hh", "A" * 43), ("level", "terminals"), ("person", "p_x"), ("device_id", "rd_" + "1" * 32)):
        exc = world.refusal("prepare", op="log", args={"ref": key, "text": "x"}, idem="a_" + "0" * 26, **{field: value})
        assert exc.code == "invalid.input", field
    assert world.click.signatures == 0


def test_closed_stream_refused(world, human):
    _, key = human.ticket()
    p = world.prepare("log", {"ref": key, "text": "x"})
    digest = world.review(p["request_id"])
    del world.streams.open[SID]  # the host closed it
    for op, args in (
        ("sign", {"request_id": p["request_id"], "view_digest": digest}),
        ("view", {"request_id": p["request_id"]}),
        ("prepare", {"op": "log", "args": {"ref": key, "text": "y"}, "idem": "a_" + "0" * 26}),
        ("presence_nonce", {}),
        ("status", {}),
    ):
        assert world.refusal(op, **args).code == "remote.no_stream", op
    assert world.click.signatures == 0


def test_a_stream_flag_needs_remote_true(world):
    with pytest.raises(ClickError) as caught:
        world.signer.handle("status", {"workspace_id": world.wid, "stream": SID})
    assert caught.value.code == "invalid.input"
    assert world.streams.asked == []


def test_view_level_signs_nothing(world, human):
    _, key = human.ticket()
    p = world.prepare("log", {"ref": key, "text": "x"})  # prepared at level work
    digest = world.review(p["request_id"])
    world.streams.open[SID]["level"] = "view"
    assert world.refusal("prepare", op="log", args={"ref": key, "text": "y"}, idem="a_" + "0" * 26).code == (
        "remote.kind_refused"
    )
    assert world.refusal("sign", request_id=p["request_id"], view_digest=digest).code == "remote.kind_refused"
    assert world.refusal("stash").code == "remote.kind_refused"
    assert world.refusal("presence_nonce").code == "remote.kind_refused"
    assert world.refusal("presence_take", digest="sha256:" + "1" * 64).code == "remote.kind_refused"
    assert world.call("view", request_id=p["request_id"])["view_digest"]  # reading is fine
    assert world.click.signatures == 0


def test_stepup_and_sealed_refused_in_stream(world, human):
    """There are no passkeys in a stream: a step-up proof or any sealed value is refused, never checked."""
    _, key = human.ticket()
    p = world.prepare("log", {"ref": key, "text": "x"})
    digest = world.review(p["request_id"])
    for extra in ({"sealed": {}}, {"stepup": {"nonce": "x"}}, {"reveal_key": "A" * 43}, {"deposit": True}):
        for op, args in (
            ("sign", {"request_id": p["request_id"], "view_digest": digest}),
            ("prepare", {"op": "log", "args": {"ref": key, "text": "y"}, "idem": "a_" + "0" * 26}),
        ):
            assert world.refusal(op, **args, **extra).code == "remote.kind_refused", (op, extra)
    assert world.refusal("stepup", op="custody_on", view_digest=digest).code == "remote.kind_refused"
    assert world.click.signatures == 0


def test_person_mismatch_refused(world, human):
    """A device of owner B must never sign as owner A: the facts' person must be the signer's person."""
    _, key = human.ticket()
    world.streams.open[SID]["person"] = "p_" + "9" * 32
    assert world.refusal("prepare", op="log", args={"ref": key, "text": "x"}, idem="a_" + "0" * 26).code == (
        "remote.kind_refused"
    )
    assert world.refusal("status").code == "remote.kind_refused"
    assert world.click.signatures == 0


def test_facts_of_another_workspace_or_a_bad_shape_are_refused(world):
    world.streams.open[SID]["workspace_id"] = "9" * 32
    assert world.refusal("status").code == "remote.kind_refused"
    world.streams.add(SID, level="root")
    assert world.refusal("status").code == "remote.kind_refused"
    world.streams.add(SID, hh="short")
    assert world.refusal("status").code == "remote.kind_refused"


def test_a_plain_grant_is_never_a_streams(world, human):
    """A single ``grant`` is always shown in the page (reveal) or deposited: both refused."""
    _, key = human.ticket()
    for kw in ({}, {"deposit": True}, {"reveal_key": "A" * 43}):
        exc = world.refusal("prepare", op="grant", args={"agent": "claude"}, idem="a_" + "0" * 26, **kw)
        assert exc.code == "remote.kind_refused", kw
