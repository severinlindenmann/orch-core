"""The owner's live run (2026-10-08): a Dark start, the planner made two children and auto-approved them, then the
epic page offered "Re-approve the epic"; the plain re-approve signed a charter without the delegation, the run
vanished from Factories and the children worked on with nothing to release or close them. Reproduced from the
planner flow (tests/factory_e2e.py), then each fix: why a re-approval was offered, re-signing the same charter,
ending a run only on purpose, and ended runs staying visible."""
import shutil

import pytest

from orch.core import epics, factory_sessions as fs, store
from factory_e2e import STOP, Script
from test_factory_e2e import make_world  # noqa: F401  (the fixture)

pytestmark = pytest.mark.skipif(not shutil.which("git"), reason="needs git")


@pytest.fixture
def planned(make_world):
    """The live run up to 04:57: the planner split the epic into two children and auto-approved them; their workers
    started and wait at their prompt."""
    w = make_world({"release": "merge"})
    w.scripts["worker"] = lambda a: Script(a, [("stop", lambda: STOP)])
    w.settle(release=False)
    kids = [c.id for c in epics.children(w.ws, w.epic)]
    assert len(kids) == 2
    return w


def _epic(w):
    return store.load(w.ws, w.epic)[1]


def test_planner_children_are_covered_by_the_running_charter_and_no_reapproval_is_offered(planned):
    w = planned
    from orch.core.query import needs_you
    from orch.dashboard.data import epic as epic_data
    from orch.dashboard.data.cards import Cards
    e = _epic(w)
    s = epics.summary(w.ws, e)
    assert {c["state"] for c in s["children"]} == {"delegated"}
    assert s["delegation"]["active"] and not s["delegation"]["epic_changed"]
    entries = store.scan(w.ws)
    needs = needs_you(w.ws, entries=entries)
    assert not [n for n in needs if n["kind"] == "approve-epic"]  # Today asks nothing
    from orch.core.events import read_events
    page = epic_data.page_data(w.ws, e, entries=entries, needs=needs, events=read_events(w.ws),
                               builder=Cards(w.ws, entries=entries, needs=needs))
    assert page["reapprove"] is False  # the epic page offered "Re-approve the epic" here: the live trap
