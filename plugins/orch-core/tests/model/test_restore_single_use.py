"""A signed decision is single-use by its id across a ``restore`` (ticket-format §5.10, O5): an id listed in
``abandoned_decisions`` stays taken on the new chain, in ``admit`` and on replay alike."""

import copy

import pytest

from orch.canon import event_head
from orch.model import admit
from orch.model.codes import Code

from .f1_runner import load, run_scenario, state_of


@pytest.fixture(scope="module")
def after_restore():
    sc = next(s for s in load("questions.json")["scenarios"] if s["name"] == "question_replayed_decision_after_restore")
    ws, tl = run_scenario(sc)
    ((uid, log),) = tl.items()
    return sc, ws, uid, log, sc["abandoned_answer"]


@pytest.mark.parametrize("base", [-1, -2, 0])
def test_an_abandoned_decision_id_is_refused_whatever_it_is_based_on(after_restore, base):
    sc, ws, uid, log, gone = after_restore
    st = state_of(sc, ws, {uid: log})
    e = copy.deepcopy(gone)
    e.update(seq=log[-1]["seq"] + 1, prev=event_head(log[-1]), based_on=event_head(log[base]), ws_seq=ws[-1]["seq"])
    e["at"] = "2099-01-01T00:00:00Z"
    e.pop("host_sig", None)
    r = admit(st, e, log=uid)
    assert getattr(r, "code", None) == Code.EVENT_DUPLICATE_ID
