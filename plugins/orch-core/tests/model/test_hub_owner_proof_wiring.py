"""#1198 C9: how the model asks for the owner proof of ``hub.joined`` / ``hub.left`` (hub contract 9.6 "Readers"). The
proof itself is the verifier's (``HubModelChecks``, tests/hub); here are the inputs only the model can supply: the
actor's ``PK`` from the replayed roster, its own record of the prior hub event, the append time of the chained line
(``at``, never a body field) and its dev state; and that a refusal changes nothing."""

from __future__ import annotations

import pytest

from orch.model import Code
from orch.model.types import ts
from tests.model.test_hub_events import HUB, ROOT
from tests.model.world import World

SIG = "A" * 86  # the schema's shape of a signature


@pytest.fixture
def w():
    return World().bootstrap({"mara": "maintainer"})


def join(w, seq=1, mode="selective"):
    return w.try_(
        "workspace", "hub.joined", "sev", hub_id=HUB, hk_root_pub=ROOT, mode=mode, binding_seq=seq, aliases=[],
        owner_proof={"o": {}, "sig": SIG},
    )  # fmt: skip


def test_the_verifier_gets_the_roster_key_the_prior_event_and_the_lines_own_time(w):
    e, res = join(w)
    assert not hasattr(res, "code")
    first = w.verifier.proof_calls[-1]
    assert first["pk_pub"] == "pk-sev" or first["pk_pub"] == w.state()._core.ws.members[w.people["sev"]].pk_pub
    assert first["prior"] is None and first["dev"] is False
    assert first["at_ms"] == ts(e["at"]) * 1000  # the chained line's time, not a field the proof or the body names
    assert first["actor_person_id"] == w.people["sev"].removeprefix("p_")
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1, owner_proof={"o": {}, "sig": SIG})
    w.state()  # replay asks for every hub event again, in order
    assert w.verifier.proof_calls[-1]["prior"] == {
        "type": "hub.joined",
        "hub_id": HUB,
        "mode": "selective",
        "binding_seq": 1,
    }
    join(w, seq=2)
    assert w.verifier.proof_calls[-1]["prior"] == {"type": "hub.left", "hub_id": HUB, "mode": None, "binding_seq": 1}


def test_a_refused_proof_is_hub_invalid_with_the_code_and_changes_nothing(w):
    e = w.build_unappended(
        "workspace", "hub.joined", "sev", hub_id=HUB, hk_root_pub=ROOT, mode="selective", binding_seq=1, aliases=[]
    )
    w.verifier.proof_codes[e["id"]] = "proof_missing"
    from orch.model import admit

    res = admit(w.state(), e, log="workspace")
    assert res.code == Code.HUB_INVALID and "proof_missing" in res.detail
    assert w.state().workspace.hub == {}


def test_a_verifier_without_the_check_refuses_every_hub_event(w):
    from orch.model import workspace
    from orch.model.verifier import RefuseHub

    e = w.build_unappended("workspace", "hub.left", "sev", hub_id=HUB, binding_seq=1, owner_proof=None)
    ws = w.state()._core.ws
    for v in (RefuseHub(), object()):
        r = workspace.hub_owner_proof(ws, e, v, dev=False)
        assert r.code == Code.HUB_INVALID and "unavailable" in r.detail


def test_the_stored_binding_keeps_the_proof_for_hub_connect(w):
    proof = {"o": {"x": 1}, "sig": SIG}
    w.wev(
        "hub.joined",
        "sev",
        hub_id=HUB,
        hk_root_pub=ROOT,
        mode="selective",
        binding_seq=1,
        aliases=[],
        owner_proof=proof,
    )
    assert w.state().workspace.hub["owner_proof"] == proof
    w.wev("hub.left", "sev", hub_id=HUB, binding_seq=1, owner_proof=proof)
    assert w.state().workspace.hub["owner_proof"] is None


def test_a_non_owner_is_a_role_error_before_the_proof_is_asked(w):
    e, res = w.try_(
        "workspace", "hub.joined", "mara", hub_id=HUB, hk_root_pub=ROOT, mode="selective", binding_seq=1, aliases=[]
    )
    assert res.code in (Code.ROLE_DENIED, Code.EVENT_BAD_ACTOR)
    assert not w.verifier.proof_calls
