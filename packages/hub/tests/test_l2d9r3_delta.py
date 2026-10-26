# ruff: noqa: F811  (the `shub` fixture is imported then named as an argument)
"""L2 delta review of the D9 round-3 fix (orch-hub#23 @ 69d54f2), l2-d13."""

from __future__ import annotations

from shares_helpers import body, shub  # noqa: F401

from orch_hub.contract import vectorgen as G
from orch_hub.contract.hub import Refused


class _Seals:
    def retire(self, ws):
        raise Refused("internal")  # what WkSeals.retire raises on any sqlite error


class _Spaces:
    def __init__(self):
        self.left: list[str] = []

    def on_unbind(self, ws):
        self.left.append(ws)


def test_d9r3_1_unbind_leaves_spaces_even_when_wk_seal_retire_fails(shub):
    sp = _Spaces()
    h = shub(wk_seals=_Seals(), spaces=sp)
    c = h.join()
    c.request("share_put", body())
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    assert sp.left == [G.WS_A]
    assert h.share_store.usage(G.WS_A)[0] == 0
