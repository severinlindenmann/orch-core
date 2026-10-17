"""Batch 11, finding 9: one review, N signatures (the signer's sequence engine, host/sequence.py)."""

from __future__ import annotations

import copy

import pytest

from orch.host import sequence, signer
from orch.host.sequence import Aborted, Driver, Port, Sessions
from tests.host import human_helpers
from tests.host.human_helpers import ok

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

pytestmark = pytest.mark.slow  # a host


def intent_of(**over):
    base = {
        "v": 1,
        "kind": "new_ticket",
        "title": "Add a sequence",
        "ticket_type": "feature",
        "set": {"labels": ["a", "b"], "priority": "high"},
        "sections": {"summary": "One line.", "context": "Line one\nLine two"},
    }
    return {**base, **over}


class Rig:
    """The terminal-shaped port over a served host: every call is the signer's own, on one connection."""

    def __init__(self, human, tmp_path, intent=None):
        self.h, self.intent = human, sequence.validate_intent(intent or intent_of())
        self.person = human.person.ref
        self.wid = human.served.wid
        self.sessions = Sessions(tmp_path / "state")
        self.submitted: list[tuple[str, list, str]] = []
        self.prepared: list[dict] = []
        self.hooks: dict = {}
        self.port = Port(
            prepare=self.prepare,
            check=human.check,
            sign=lambda p: human.sign(p).events,
            submit=self.submit,
            head=lambda key: ok(human.c.req("ticket.get", {"ref": key}))["data"]["doc"]["head"],
            new_idem=lambda: __import__("orch.host.control_client", fromlist=["x"]).new_idem(),
            cancel=lambda rid: human.c.req("human.cancel", {"request_id": rid}),
        )

    def meta0(self):
        return {
            "step": 0,
            "of": len(sequence.steps(self.intent)),
            "digest": sequence.digest_of(self.wid, self.person, self.intent),
            "intent": self.intent,
        }

    def first(self):
        op, args = sequence.step_args(self.intent, 0, None)
        res = self.h.c.req("human.prepare", {"op": op, "args": args, "signer": self.h.signer, "sequence": self.meta0()})
        return ok(res)["data"]

    def prepare(self, op, args, meta):
        res = self.h.c.req("human.prepare", {"op": op, "args": args, "signer": self.h.signer, "sequence": meta})
        if "error" in res:
            print("PREPARE ERROR", res["error"])
        got = ok(res)["data"]
        self.prepared.append(got)
        return got

    def submit(self, rid, events, idem):
        hook = self.hooks.get(len(self.submitted))
        self.submitted.append((rid, events, idem))
        if hook is not None:
            hook(rid, events, idem)
        res = ok(self.h.submit_res(rid, events, idem))
        return {**res["data"], "seq": res.get("seq", res["data"].get("seq"))}

    def driver(self):
        return Driver(self.port, self.sessions, workspace_id=self.wid, person=self.person, intent=self.intent)


def doc(human, key):
    return ok(human.c.req("ticket.get", {"ref": key}))["data"]


def test_one_intent_signs_created_then_the_sections(human, tmp_path):
    rig = Rig(human, tmp_path)
    out = rig.driver().run(rig.first())
    assert out["landed"] == 4  # new, set, two sections
    assert len(rig.submitted) == 4
    got = doc(human, out["key"])
    assert got["doc"]["title"] == "Add a sequence" and got["doc"]["priority"] == "high"
    assert got["doc"]["labels"] == ["a", "b"]
    # each later event is based on the head of the event the signer signed before it (recorded in the session)
    rec = rig.sessions.get(rig.wid, rig.driver().digest)
    based = [ev["based_on"] for _, events, _ in rig.submitted for ev in events]
    assert based[1:] == [s["head"] for s in rec["steps"][:-1]]
    assert [s["state"] for s in rec["steps"]] == ["landed"] * 4


def test_the_first_request_is_one_review_of_every_step(human, tmp_path):
    from orch.host import signcontent

    rig = Rig(human, tmp_path)
    first = rig.first()
    human.check(first)
    content = signcontent.build(first, verified=True)
    titles = [s["title"] for s in content["sections"]]
    assert titles == ["Context", "Summary"] or sorted(titles) == ["Context", "Summary"]
    assert "Create ticket" in content["headline"]
    assert any("4 steps" in n for n in content["notes"])
    text = signer.render(first, lambda w: ("ws", "ORCH"))
    assert "Line two" in text and "4 signatures, ONE confirmation" in text
    assert text.count("--- event ") == 4


def test_an_intent_mismatch_aborts_before_the_signature(human, tmp_path):
    rig = Rig(human, tmp_path)
    first = rig.first()
    real = rig.prepare

    def evil(op, args, meta):
        if meta["step"] == 2:
            args = {**args, "message": args["message"] + " (changed by the host)"}  # another text than reviewed
        return real(op, args, meta)

    rig.port.prepare = evil
    with pytest.raises(Aborted, match="ticket changed while signing"):
        rig.driver().run(first)
    assert len(rig.submitted) == 2  # steps 0 and 1 landed; step 2 was never signed
    assert len(human.backend.payloads) == 2
    assert doc(human, rig.sessions.get(rig.wid, rig.driver().digest)["ticket"]["key"])["doc"]["priority"] == "high"
    assert rig.sessions.get(rig.wid, rig.driver().digest)["aborted"]


def test_a_foreign_event_in_between_aborts(human, tmp_path):
    rig = Rig(human, tmp_path)
    first = rig.first()

    def foreign(rid, events, idem):  # after step 0 landed? no: before it. use hook at index 1 (after step 0)
        pass

    state = {}

    def after_first(rid, events, idem):
        state["go"] = True

    rig.hooks[0] = after_first
    real_prepare = rig.prepare

    def prepare(op, args, meta):
        if meta["step"] == 1:  # a foreign agent comment lands between step 0 and step 1
            human.agent("log", {"ref": args["ref"], "message": "foreign"})
        return real_prepare(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert len(rig.submitted) == 1  # only the ticket exists: a visible draft
    key = rig.sessions.get(rig.wid, rig.driver().digest)["ticket"]["key"]
    assert doc(human, key)["doc"]["title"] == "Add a sequence"


def test_crash_after_step_one_resends_the_exact_bytes_and_continues(human, tmp_path):
    rig = Rig(human, tmp_path)
    first = rig.first()

    class Boom(BaseException):
        pass

    # the host answer to step 1's submit is lost (the process dies after the bytes were signed and recorded)
    def die(rid, events, idem):
        raise Boom

    rig.hooks[1] = die
    with pytest.raises(Boom):
        rig.driver().run(first)
    rec = rig.sessions.get(rig.wid, rig.driver().digest)
    assert [s["state"] for s in rec["steps"]] == ["landed", "signed"]
    signed_bytes = copy.deepcopy(rec["steps"][1]["events"])
    # the person asks again: a new first request is prepared and cancelled, nothing new is signed for step 0 or 1
    again = rig.first()
    signs_before = len(human.backend.payloads)
    rig.hooks.clear()
    out = rig.driver().run(again)
    assert out["landed"] == 4
    resent = [s for s in rig.submitted if s[1] == signed_bytes]
    assert len(resent) == 2 and resent[0][2] == resent[1][2]  # the same bytes and the same idem
    assert len(human.backend.payloads) == signs_before + 2  # only steps 2 and 3 are new signatures
    # one ticket only: the second prepare of `new` was cancelled
    assert doc(human, out["key"])["doc"]["priority"] == "high"


def test_resume_with_a_foreign_head_aborts_and_leaves_a_draft(human, tmp_path):
    rig = Rig(human, tmp_path)
    first = rig.first()

    class Boom(BaseException):
        pass

    def die(rid, events, idem):
        raise Boom

    rig.hooks[1] = die
    with pytest.raises(Boom):
        rig.driver().run(first)
    key = rig.sessions.get(rig.wid, rig.driver().digest)["ticket"]["key"]
    rig.hooks.clear()
    # step 1 DID land at the host (only the answer was lost), then somebody else wrote: the head is foreign
    ok(human.submit_res(rig.submitted[1][0], rig.submitted[1][1], rig.submitted[1][2]))
    human.agent("log", {"ref": key, "message": "foreign"})
    with pytest.raises(Aborted):
        rig.driver().run(rig.first())
    assert len(rig.submitted) <= 4
    assert doc(human, key)["doc"]["title"] == "Add a sequence"


def test_a_claim_without_signed_bytes_is_never_signed_again(human, tmp_path):
    rig = Rig(human, tmp_path)
    first = rig.first()

    def crash_sign(p):
        raise RuntimeError("the process died while signing")

    rig.port.sign = crash_sign
    with pytest.raises(Aborted):
        rig.driver().run(first)
    rec = rig.sessions.get(rig.wid, rig.driver().digest)
    assert rec["steps"][0]["state"] == "claimed"
    assert rec["aborted"]


def test_confirmation_strength_is_the_strongest_step(human):
    assert sequence.confirm_kind(sequence.validate_intent(intent_of())) == "click"


def test_the_host_refuses_a_later_step_on_a_foreign_head(human, tmp_path):
    rig = Rig(human, tmp_path)
    out = rig.driver().run(rig.first())
    res = human.c.req(
        "human.prepare",
        {
            "op": "section.set",
            "args": {"ref": out["key"], "section": "plan", "message": "x"},
            "signer": human.signer,
            "sequence": {"step": 1, "of": 4, "digest": "sha256:" + "0" * 64, "prev": "0" * 26},
        },
    )
    assert res["error"]["code"] == "human.request_mismatch"


# --- the terminal path: `orch sign CODE` on the first request ------------------------------------------------------


def handoff_first(rig):
    op, args = sequence.step_args(rig.intent, 0, None)
    res = rig.h.c.req("human.prepare", {"op": op, "args": args, "handoff": True, "sequence": rig.meta0()})
    return ok(res)["data"]


def terminal(rig, served, shown, code, *, backend=None, sessions=None):
    from orch.host import handoff
    from orch.host.control_client import new_idem
    from tests.host.test_handoff import connect

    with connect(served) as conn:
        return handoff.sign_code(
            conn.call,
            code,
            backend=backend or rig.h.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=rig.person,
            device=rig.h.person.device,
            workspace=lambda w: ("Acme", "DEMO") if w == served.wid else None,
            confirm=shown.confirm,
            show_secret=shown.secret,
            idem=new_idem(),
            sessions=sessions or rig.sessions,
        )


def test_terminal_one_review_one_word_and_one_unlock_sign_every_step(human, served, tmp_path):
    from tests.host.test_handoff import Shown

    rig = Rig(human, tmp_path)
    p = handoff_first(rig)
    unlocks = []

    class OpenOnce:  # a custody with a batch unlock: the passphrase is asked once for the whole sequence
        name, person_capable, auth = "test", True, "passphrase"

        def unlocked(self, key_id, *, action, digest, labels, fields=()):
            import contextlib

            unlocks.append((action, digest, fields, labels))

            @contextlib.contextmanager
            def cm():
                yield lambda payload: human.person.dk_sign(payload)

            return cm()

    shown = Shown()
    result = terminal(rig, served, shown, p["request"]["code"], backend=OpenOnce())
    assert result["steps"] == 4 and result["key"]
    assert len(shown.words) == 1 and len(unlocks) == 1  # one confirmation, one unlock
    assert "Line two" in shown.plain[0] and "Add a sequence" in shown.plain[0]  # the text is IN the one review
    assert doc(human, result["key"])["doc"]["priority"] == "high"
    assert unlocks[0][2][0] == ("signatures", "4")


def test_terminal_not_confirmed_signs_nothing(human, served, tmp_path):
    from tests.host.test_handoff import Shown

    rig = Rig(human, tmp_path)
    p = handoff_first(rig)
    shown = Shown(answer=False)
    with pytest.raises(signer.SignerRefused, match="not confirmed"):
        terminal(rig, served, shown, p["request"]["code"])
    assert human.backend.payloads == []


def test_terminal_a_host_that_shows_another_intent_than_it_prepared_is_refused(human, served, tmp_path):
    from orch.host import handoff
    from orch.host.control_client import new_idem
    from tests.host.test_handoff import Shown

    rig = Rig(human, tmp_path)
    p = handoff_first(rig)

    def call(op, args, idem=None, files=None):
        res = ok(human.c.req(op, args, idem=idem, files=files))
        if op == "human.get":  # the review digest covers the intent, so changing it needs the digest changed too
            res["data"]["material"]["sequence"]["intent"]["sections"]["summary"] = "something else"
        return res

    shown = Shown()
    with pytest.raises(signer.SignerRefused):
        handoff.sign_code(
            call,
            p["request"]["code"],
            backend=human.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=rig.person,
            device=human.person.device,
            workspace=lambda w: ("Acme", "DEMO"),
            confirm=shown.confirm,
            show_secret=shown.secret,
            idem=new_idem(),
            sessions=rig.sessions,
        )
    assert shown.plain == [] and human.backend.payloads == []


def test_terminal_a_client_without_sessions_refuses(human, served, tmp_path):
    from orch.host import handoff
    from orch.host.control_client import new_idem
    from tests.host.test_handoff import Shown, connect

    rig = Rig(human, tmp_path)
    p = handoff_first(rig)
    with connect(served) as conn, pytest.raises(signer.SignerRefused, match="cannot sign sequences"):
        handoff.sign_code(
            conn.call,
            p["request"]["code"],
            backend=human.backend,
            key_id="dk",
            workspace_id=served.wid,
            person=rig.person,
            device=human.person.device,
            workspace=lambda w: ("Acme", "DEMO"),
            confirm=Shown().confirm,
            show_secret=lambda t: None,
            idem=new_idem(),
        )
