"""#740 manual acceptance through the dashboard's hand-off check, fed by the REAL host's `human.prepare`.

test_manual_acceptance.py checks the `expected()` / `expected_material()` predicates on hand-built events; this module
runs core's in-process host (tests.host.human_helpers) and passes what it prepares through
`adapters.handoff.request` exactly as the /handoff and click routes do, so a drift between the host's `Material` and
the dashboard's check (the e2e "does not match its own hashes" refusal) fails here.
"""

import copy
from types import SimpleNamespace

import pytest
from pydantic import TypeAdapter

from orch_dashboard import adapters
from orch_dashboard.errors import HostError
from orch_dashboard.signing import PrepareBody, expected, expected_material, host_call
from tests.host import human_helpers

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human


@pytest.fixture(autouse=True)
def host_clock(request, monkeypatch):
    """The dashboard checks `expires_at` against its clock: give it the test host's (fake) clock."""
    if "human" in request.fixturenames:
        lab = request.getfixturevalue("lab")
        monkeypatch.setattr(adapters.handoff, "time", SimpleNamespace(time=lambda: float(lab.ws.clock[0])))


def parse(body):
    return TypeAdapter(PrepareBody).validate_python(body)


def through_dashboard(human, body):
    """`human.prepare` (handoff) for the action, then the dashboard's own check of the answer (handoff.py prepare)."""
    action = parse(body)
    op, args = host_call(action)
    prepared = human.prepare(op, args, handoff=True)
    return prepared, adapters.handoff.request(
        prepared,
        human.served.wid,
        expect_op=op,
        expect_key=getattr(action, "key", None),
        expect_events=expected(action, human.person.ref),
        expect_material=expected_material(action),
    )


def criterion(human):
    _, key = human.ticket()
    human.run("ac.add", {"ref": key, "text": "Login shows an error for a wrong password"})
    return key


@pytest.mark.parametrize("note", [None, "I tried it"])
def test_accept_withdraw_and_accept_again_pass_the_dashboard_check(human, note):
    key = criterion(human)
    accept = {"action": "accept_criterion", "key": key, "ac": "AC1"} | ({"note": note} if note else {})
    prepared, shown = through_dashboard(human, accept)
    assert shown["op"] == "ac.accept" and shown["events"] == prepared["events"]
    assert shown["material"]["statement"] == prepared["material"]["statement"]
    human.run("ac.accept", {"ref": key, "ac": "AC1"} | ({"note": note} if note else {}))

    _, shown = through_dashboard(human, {"action": "withdraw_acceptance", "key": key, "ac": "AC1", "reason": "recheck"})
    assert shown["op"] == "ac.unaccept"
    human.run("ac.unaccept", {"ref": key, "ac": "AC1", "reason": "recheck"})

    _, shown = through_dashboard(human, accept)  # accept again after a withdrawal
    assert shown["events"][0]["name"] == "accept-AC1-2.md"


def test_mark_task_done_passes_the_dashboard_check(human):
    key = criterion(human)
    human.run("task.add", {"ref": key, "text": "Do it", "proves": ["AC1"]})
    _, shown = through_dashboard(human, {"action": "mark_task_done", "key": key, "task": "T1"})
    assert shown["op"] == "task.mark_done"


def test_a_statement_is_still_checked_against_what_was_asked(human):
    """The fix admits the host's `statement` key; it does not loosen what the dashboard checks about it."""
    key = criterion(human)
    action = parse({"action": "accept_criterion", "key": key, "ac": "AC1", "note": "I tried it"})
    op, args = host_call(action)
    prepared = human.prepare(op, args, handoff=True)

    def check(p, act=action):
        return adapters.handoff.request(
            p,
            human.served.wid,
            expect_op=op,
            expect_key=key,
            expect_events=expected(act, human.person.ref),
            expect_material=expected_material(act),
        )

    other_note = parse({"action": "accept_criterion", "key": key, "ac": "AC1", "note": "something else"})
    with pytest.raises(HostError):
        check(prepared, other_note)
    for mutate in (
        lambda m: m.pop("statement"),
        lambda m: m.__setitem__("statement", m["statement"] + "x"),
        lambda m: m.__setitem__("extra", 1),
    ):
        p = copy.deepcopy(prepared)
        mutate(p["material"])
        with pytest.raises(HostError):
            check(p)


def test_a_statement_is_refused_on_any_other_action(human):
    """Only a manual acceptance (whose action checks the statement) may carry one: on another op it is refused."""
    key = criterion(human)
    action = parse({"action": "comment", "key": key, "message": "hello"})
    op, args = host_call(action)
    prepared = human.prepare(op, args, handoff=True)
    adapters.handoff.request(prepared, human.served.wid, expect_op=op, expect_key=key)  # as prepared: fine
    stolen = human.prepare("ac.accept", {"ref": key, "ac": "AC1"}, handoff=True)["material"]["statement"]
    p = copy.deepcopy(prepared)
    p["material"]["statement"] = stolen
    with pytest.raises(HostError):
        adapters.handoff.request(p, human.served.wid, expect_op=op, expect_key=key)


def test_verify_gate_material_with_manual_acceptances_is_a_known_shape():
    """A verify approval or verdict carries the host's `manual_acceptances` list (core a7037ef9, L1): the dashboard's
    Material schema admits it there, closed, and nowhere else."""
    ok = {"kind": "gate", "gate": "verify", "input": {}, "policy": {}, "texts": {}, "ticket": None, "review": []}
    row = {"ac": "AC1", "name": "accept-AC1-1.md", "by": "p_" + "1" * 32}
    assert adapters.handoff.material_keys_ok(ok | {"manual_acceptances": []})
    assert adapters.handoff.material_keys_ok(ok | {"manual_acceptances": [row]})
    assert not adapters.handoff.material_keys_ok(ok | {"gate": "requirements", "manual_acceptances": []})
    assert not adapters.handoff.material_keys_ok(ok | {"manual_acceptances": [row | {"x": "y"}]})
    assert not adapters.handoff.material_keys_ok(ok | {"manual_acceptances": [{"ac": "AC1"}]})
    assert not adapters.handoff.material_keys_ok(ok | {"manual_acceptances": "AC1"})


def test_the_material_schema_is_the_signers():
    from orch.host import signer

    for kind, keys in adapters.handoff.MATERIAL_KEYS.items():
        assert keys == signer._MATERIAL_KEYS[kind], kind
