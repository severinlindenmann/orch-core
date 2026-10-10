"""advance/at are incremental: one append at 1000 tickets x 50 events stays in milliseconds."""

import time

import pytest

from orch.model import admit, advance, at
from tests.model.world import World, stamp
from tests.schema.examples import sig

pytestmark = pytest.mark.slow


def test_advance_and_at_stay_fast_at_scale():
    w = World(validate=False).bootstrap({"mara": "maintainer"})
    uids = [w.ticket() for _ in range(1000)]
    for uid in uids:
        for _ in range(49):
            w.tev(uid, "log.added", "sev", text="x")
    st = w.state()
    assert len(st.tickets) == 1000
    uid = uids[500]
    timings = []
    for _ in range(20):
        e = w.build_unappended(uid, "log.added", "sev", text="y")
        assert admit(st, e, log=uid).__class__.__name__ == "Ok"
        e["host_sig"] = sig("h")
        t0 = time.perf_counter()
        st = advance(st, e, log=uid)
        timings.append(time.perf_counter() - t0)
        w.tl[uid].append(e)
    assert sum(timings) / len(timings) < 0.005, timings
    t0 = time.perf_counter()
    later = at(st, stamp(w.clock))
    assert time.perf_counter() - t0 < 0.05
    assert later.tickets[uid] == w.state().tickets[uid]
