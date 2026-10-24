"""#1198 C9 (Opus delta D1): the dev state of a workspace is its signed genesis, and a refused owner hub event leaves
the hub state ``unknown`` (full), never absent. So a dev-state or proof mismatch can never read as standalone, and the
two refusal checks (One click on, key into convenience custody) hold on a REAL (non-dev) workspace."""

from __future__ import annotations

import pytest

from orch import devsandbox
from orch.host import convenience, hubops, hubsign
from orch.host.errors import HostError
from orch.hubclient import wire
from orch.model import breakglass, engine
from tests.host import human_helpers
from tests.host.lifecycle_helpers import UID
from tests.hub.vec import V

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

HUB_ID, ROOT = V["ids"]["hub"], V["keys"]["hk_root"]["pub"]
REQ = "hr_01J9ZP00000000000000000RQ1"


def full_join(served, *, dev):
    """A full ``hub.joined`` the owner's device signed with an owner proof of the given dev marker, appended raw to the
    log (the host's checks are bypassed: what judges it is the model, with the real crypto verifier)."""
    owner = served.lab.ws.owner
    payload = {"hub_id": HUB_ID, "hk_root_pub": ROOT, "mode": "full", "binding_seq": 1, "aliases": []}
    now = int(served.lab.ws.clock[0] * 1000)
    proof = wire.build_owner_proof(
        workspace_id=served.wid, pk_pub=owner.pk_pub, op="hub.join", payload=payload, request_id=REQ,
        issued_ms=now, expires_ms=now + 600_000, dev=dev, sign=owner.pk_sign,
    )  # fmt: skip

    def run(store):
        served.lab.ws.tick()
        return store.append(
            served.lab.ws.person_event(owner, "workspace", "hub.joined", **payload, owner_proof=proof), log="workspace"
        )

    return served.append(run)


def hub_of(served):
    store = served.lab.ws.other()
    try:
        return dict(store.state.workspace.hub)
    finally:
        store.close()


def entry(served):
    return {"workspace_id": served.wid, "root": served.lab.root}


def refusals_hold(served):
    for fn in (
        lambda: convenience.refuse_full_mode_entry(served.lab.state, entry(served), UID),
        lambda: convenience.refuse_turn_on(served.lab.state, entry(served), UID, person=served.lab.owner),
    ):
        with pytest.raises(HostError) as e:
            fn()
        assert e.value.code == "sign.hub_only"


def test_the_dev_prefix_is_the_sandbox_prefix():
    assert engine.DEV_PREFIX == devsandbox.RESERVED_PREFIX


def test_a_real_workspace_joined_in_full_mode_refuses_one_click_and_convenience_custody(served):
    full_join(served, dev=False)  # a real proof on a real workspace: valid
    hub = hub_of(served)
    assert hub["status"] == "joined" and not hub.get("invalid")
    refusals_hold(served)


def test_a_dev_proof_on_a_real_workspace_is_refused_by_the_model_and_the_refusals_still_hold(served, monkeypatch):
    """The mismatch. The append refuses it; the log of an attacker or a bug can still hold such a line, so it is
    written without the admission check: the replay must read the workspace as unknown (full), never standalone."""
    from orch.store import StoreError

    with pytest.raises(StoreError) as e:
        full_join(served, dev=True)
    assert "dev_refused" in str(e.value)
    # the same line, in the log without the admission (an old or hand-made log): the replay refuses it, state unknown
    from orch.model import admit as real_admit

    monkeypatch.setattr("orch.store.store.admit", lambda state, ev, **kw: None, raising=False)
    try:
        full_join(served, dev=True)
    finally:
        monkeypatch.undo()
    hub = hub_of(served)
    assert hub.get("invalid") is True and hub.get("status") != "joined"
    signing = served.service.submit(hubops.signing_mode, served.service).result()
    assert signing == "unknown" and "unknown" in hubsign.HUB_ONLY
    assert real_admit is not None
    refusals_hold(served)


def test_unknown_counts_as_full_for_every_model_and_host_reader():
    from tests.model.world import World

    w = World().bootstrap({})
    # a refused owner hub.joined: appended without admission (replay refuses it)
    ev = w.build_unappended(
        "workspace", "hub.joined", "sev", hub_id=HUB_ID, hk_root_pub=ROOT, mode="full", binding_seq=1, aliases=[]
    )
    w.verifier.proof_codes[ev["id"]] = "dev_refused"
    w.ws.append({**ev, "host_sig": "x" * 86})
    ws = w.state()._core.ws
    assert ws.hub.get("invalid") is True and not ws.hub.get("status")
    assert breakglass.hub_only_position(ws) and hubsign.log_hub_only(ws)


def test_a_refused_event_of_a_non_owner_changes_nothing():
    from tests.model.world import World

    w = World().bootstrap({"mara": "member"})
    ev = w.build_unappended("workspace", "hub.left", "mara", hub_id=HUB_ID, binding_seq=1)
    w.ws.append({**ev, "host_sig": "x" * 86})
    assert w.state()._core.ws.hub == {}  # a member could not leave or join anything: no lock-out lever


def test_a_later_valid_join_clears_the_mark():
    from tests.model.world import World

    w = World().bootstrap({})
    bad = w.build_unappended(
        "workspace", "hub.joined", "sev", hub_id=HUB_ID, hk_root_pub=ROOT, mode="full", binding_seq=1, aliases=[]
    )
    w.verifier.proof_codes[bad["id"]] = "proof_invalid"
    w.ws.append({**bad, "host_sig": "x" * 86})
    assert w.state()._core.ws.hub.get("invalid") is True
    w.wev("hub.joined", "sev", hub_id=HUB_ID, hk_root_pub=ROOT, mode="selective", binding_seq=1, aliases=[])
    hub = w.state()._core.ws.hub
    assert hub["status"] == "joined" and not hub.get("invalid")
