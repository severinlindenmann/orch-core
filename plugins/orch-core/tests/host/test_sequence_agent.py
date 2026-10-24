"""U4 PR1: a new ticket may end with the runner grant (the optional ``agent`` part), signed LAST in the same one
confirmation. One test per invariant of ~/orch-dev/state/design-u4-create-and-start.md (I1 to I6)."""

from __future__ import annotations

import copy

import pytest

from orch.host import seqquick, sequence, signcontent, signer
from orch.host.sequence import Aborted
from tests.host import human_helpers
from tests.host.dispatch_helpers import SESSION
from tests.host.test_sequence import Rig, doc, intent_of
from tests.host.test_sequence_quick import click_prepare, quick

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human
from tests.host.test_click import click  # noqa: E402,F401

AGENT = {"hours": 8, "scope": "workable"}

#: the digest of intent_of() for these ids on develop 4fac2abf, BEFORE the agent part existed
GOLDEN_DIGEST = "sha256:8bbb0b9172fd7b52546e005b49900ce7e3ec4e931a93f91779fafc720f0ee838"


def test_i4_an_intent_without_agent_keeps_its_exact_digest_and_steps():
    intent = sequence.validate_intent(intent_of())
    assert "agent" not in intent
    assert sequence.digest_of("a" * 32, "p_" + "1" * 32, intent) == GOLDEN_DIGEST
    assert [s["kind"] for s in sequence.steps(intent)] == ["new", "set", "section", "section"]
    assert sequence.extra_lines(intent) == []
    with_agent = sequence.validate_intent(intent_of(agent=AGENT))
    assert sequence.digest_of("a" * 32, "p_" + "1" * 32, with_agent) != GOLDEN_DIGEST
    assert [s["kind"] for s in sequence.steps(with_agent)] == ["new", "set", "section", "section", "grant"]


@pytest.mark.parametrize(
    "agent",
    [
        {"hours": 0, "scope": "workable"},
        {"hours": 169, "scope": "workable"},
        {"hours": True, "scope": "workable"},
        {"hours": 8, "scope": "all"},
        {"hours": 8, "scope": "workable", "runner": False},
        {"hours": 8},
        None,
        "workable",
    ],
)
def test_i4_the_agent_part_is_validated_by_the_quick_tasks_function(agent):
    with pytest.raises(sequence.SequenceInvalid):
        sequence.validate_intent(intent_of(agent=agent))
    with pytest.raises(sequence.SequenceInvalid):
        seqquick.validate_agent(agent, sequence.SequenceInvalid)
    assert sequence.validate_intent(intent_of(agent={"hours": 168, "scope": "workable"}))["agent"]["hours"] == 168


def test_i4_the_quick_task_and_the_new_ticket_build_the_same_grant():
    q = sequence.validate_intent(quick(agent=AGENT))
    n = sequence.validate_intent(intent_of(agent=AGENT))
    assert sequence.steps(q)[-1] == sequence.steps(n)[-1]
    assert sequence.expected_payload(q, len(sequence.steps(q)) - 1, "p") == sequence.expected_payload(
        n, len(sequence.steps(n)) - 1, "p"
    )


def test_i1_no_gate_is_approved_by_the_create_and_start_confirmation(human, lab, tmp_path):
    intent = intent_of(agent=AGENT, acceptance=["It opens"])
    assert all(
        s["op"] != "approve" and s["op"] != "agent.start" for s in sequence.steps(sequence.validate_intent(intent))
    )
    rig = Rig(human, tmp_path, intent)
    out = rig.driver().run(rig.first())
    types = [e["type"] for _, ev, _ in rig.submitted for e in ev]
    assert "gate.approved" not in types and types.count("grant.issued") == 1
    gates = doc(human, out["key"])["gates"]
    assert gates["requirements"]["state"] != "approved" and gates["plan"]["state"] != "approved"


def test_i2_the_grant_is_last_after_an_owner_change_and_every_prefix_without_it_has_no_access(human, lab, tmp_path):
    quinn = lab.add_member()
    intent = intent_of(agent=AGENT, people={"owner": quinn.ref})
    kinds = [s["kind"] for s in sequence.steps(sequence.validate_intent(intent))]
    assert kinds[-2:] == ["people", "grant"] and kinds.count("grant") == 1
    # abort right before the grant: a ticket (owner set), no grant
    rig = Rig(human, tmp_path, intent)
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        if op == "grant":
            res = human.c.req(
                "log",
                {"ref": meta["ticket"], "message": "foreign"},
                grant=human.served.grant,
                session=SESSION,
                idem=True,
            )
            assert res["ok"]
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    types = [e["type"] for _, ev, _ in rig.submitted for e in ev]
    assert types[-1] == "people.changed" and "grant.issued" not in types
    # the whole run lands, grant last
    rig2 = Rig(human, tmp_path / "b", intent)
    out = rig2.driver().run(rig2.first())
    types2 = [e["type"] for _, ev, _ in rig2.submitted for e in ev]
    assert out["landed"] == len(kinds) and types2[-1] == "grant.issued"
    assert doc(human, out["key"])["doc"]["owner"] == quinn.ref


def test_i2_a_grant_with_other_hours_or_times_is_refused_by_the_signer(human, tmp_path):
    rig = Rig(human, tmp_path, intent_of(agent=AGENT))
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        got = real(op, args, meta)
        if op == "grant":
            got = copy.deepcopy(got)
            got["events"][0]["expires_at"] = "2099-01-01T00:00:00Z"
            from orch.host import review

            got["material"]["review"] = [review.of(e, got["material"]) for e in got["events"]]
            digest = review.review_digest(got["material"])
            got["review_digest"], got["fingerprint"] = digest, review.fingerprint(digest, got["events"], "grant")
        return got

    rig.port.prepare = prepare
    with pytest.raises(Aborted, match="changed while signing"):
        rig.driver().run(first)
    assert "grant.issued" not in [e["type"] for _, ev, _ in rig.submitted for e in ev]


def test_i2_a_grant_asked_for_other_hours_than_the_reviewed_ones_is_never_signed(human, tmp_path):
    rig = Rig(human, tmp_path, intent_of(agent=AGENT))
    first = rig.first()
    real = rig.prepare

    def prepare(op, args, meta):
        if op == "grant":
            args = {**args, "hours": 9}  # not the hours that were reviewed
        return real(op, args, meta)

    rig.port.prepare = prepare
    with pytest.raises(Aborted):
        rig.driver().run(first)
    assert "grant.issued" not in [e["type"] for _, ev, _ in rig.submitted for e in ev]


def test_i3_agent_access_is_a_click_and_a_runner_grant_needs_dashboard_execution(click, served, human):  # noqa: F811
    """#1198 C1: no typed word. The rule that a grant to the runner needs dashboard execution on this machine stands on
    its own: at prepare, and again at sign (it may have been switched off in between)."""
    from tests.host.test_click import refused

    intent = sequence.validate_intent(intent_of(agent=AGENT))
    assert sequence.confirm_kind(intent) == "click" and sequence.has_grant(intent)
    plain = sequence.validate_intent(intent_of())
    assert sequence.confirm_kind(plain) == "click" and not sequence.has_grant(plain)
    # without dashboard execution on this machine: the terminal's
    click.signer._execution = lambda entry: False
    refused(lambda: click_prepare(click, intent_of(agent=AGENT)), "human.click_refused", "terminal_only")
    click.signer._execution = lambda entry: True
    p = click_prepare(click, intent_of(agent=AGENT))
    assert p["confirm"] == "click"
    v = click.view(p["request_id"])
    assert v["view"]["confirm"] == {"kind": "click"}
    click.mono.t += 2
    click.signer._execution = lambda entry: False  # switched off after the review: the click is refused, nothing signed
    refused(lambda: click.sign(p["request_id"], v["view_digest"]), "human.click_refused", "terminal_only")
    assert click.signatures == 0
    click.signer._execution = lambda entry: True
    p = click_prepare(click, intent_of(agent=AGENT))  # a refused request is spent; a fresh one signs
    v = click.view(p["request_id"])
    click.mono.t += 2
    out = click.sign(p["request_id"], v["view_digest"])
    assert click.signatures == 5 and out["grant"].startswith("gr_")  # new, set, two sections, the grant


def test_i3_the_terminal_word_is_grant(human, tmp_path):
    rig = Rig(human, tmp_path, intent_of(agent=AGENT))
    first = rig.first()
    human.check(first)
    assert signer.confirm_word(first) == "GRANT"


def test_i5_the_review_says_the_start_is_not_signed_and_names_the_grant(human, tmp_path):
    rig = Rig(human, tmp_path, intent_of(agent=AGENT))
    first = rig.first()
    human.check(first)
    content = signcontent.build(first, verified=True)
    notes = sequence.build_content(first, verified=True)["notes"]
    joined = "\n".join(notes)
    assert "Not signed: the agent start" in joined
    assert "Agents may work on any ticket they are allowed to work on" in joined and "GRANT" in joined
    assert "Nothing here approves the requirements or the plan" in joined
    assert any("5 steps" in n for n in notes)
    assert "Create DEMO-" in content["headline"]
    text = signer.render(first, lambda w: ("ws", "ORCH"))
    assert "Not signed: the agent start" in text and "5 signatures, ONE confirmation" in text
    assert not any(
        "Not signed" in n for n in sequence.build_content(Rig(human, tmp_path / "b").first(), verified=True)["notes"]
    )  # a ticket without the agent part says nothing about a start


def test_i6_the_grant_is_exactly_what_start_agent_uses_today_a_workable_runner_grant(human, tmp_path):
    rig = Rig(human, tmp_path, intent_of(agent={"hours": 3, "scope": "workable"}))
    out = rig.driver().run(rig.first())
    ev = next(e for _, evs, _ in rig.submitted for e in evs if e["type"] == "grant.issued")
    assert (ev["scope"], ev["verbs"], ev["label"], ev["hours"]) == ("workable", "agent", "runner", 3)
    assert out["landed"] == 5
