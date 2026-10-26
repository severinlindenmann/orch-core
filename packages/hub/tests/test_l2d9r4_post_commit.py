# ruff: noqa: F811  (the `shub` fixture is imported then named as an argument)
"""D9r3-1: after the registry commit every post-commit step of binding_put runs whatever another step does."""

from __future__ import annotations

import pytest
from shares_helpers import body, shub  # noqa: F401

from orch_hub.contract import vectorgen as G
from orch_hub.contract.hub import Refused


class _Seals:
    def __init__(self, fail=False):
        self.fail, self.ran = fail, False

    def retire(self, ws):
        self.ran = True
        if self.fail:
            raise Refused("internal")  # what WkSeals.retire raises on any sqlite error


class _Views:
    def __init__(self, fail=False):
        self.fail, self.dropped = fail, []
        self.store = self

    def drop(self, ws):
        self.dropped.append(ws)
        if self.fail:
            raise RuntimeError("view store down")


class _Spaces:
    def __init__(self, fail=False):
        self.fail, self.left = fail, []

    def on_unbind(self, ws):
        self.left.append(ws)
        if self.fail:
            raise RuntimeError("spaces down")


class _Down:
    def __init__(self, fail=False):
        self.fail, self.bindings = fail, []

    def card(self, *a):
        pass

    def binding(self, ws, obj):
        self.bindings.append(ws)
        if self.fail:
            raise RuntimeError("downstream down")


@pytest.mark.parametrize("failing", ["seals", "shares", "downstream", "spaces", "views"])
def test_each_post_commit_step_runs_when_another_fails(shub, failing, monkeypatch):
    seals, sp, down, vw = _Seals(), _Spaces(), _Down(), _Views()
    h = shub(wk_seals=seals, spaces=sp, downstream=down, views=vw)
    c = h.join()
    c.request("share_put", body())
    seals.fail, seals.ran = failing == "seals", False
    down.fail = failing == "downstream"  # the join above ran healthy steps
    sp.fail = failing == "spaces"
    vw.fail, vw.dropped = failing == "views", []
    if failing == "shares":
        calls = []

        def boom(ws):
            calls.append(ws)
            raise RuntimeError("share store down")

        monkeypatch.setattr(h.svc, "sync", boom)
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    assert seals.ran
    assert down.bindings[-1] == G.WS_A
    assert sp.left == [G.WS_A]
    assert vw.dropped == [G.WS_A]
    if failing == "shares":
        assert calls == [G.WS_A]
    else:
        assert h.share_store.usage(G.WS_A)[0] == 0


def test_view_drop_runs_when_the_wk_seal_retire_fails(shub):
    seals, vw = _Seals(fail=True), _Views()
    h = shub(wk_seals=seals, views=vw)
    c = h.join()
    vw.dropped.clear()
    assert c.request("binding_put", {"object": G.unbound(seq=1)}) == {}
    assert seals.ran and vw.dropped == [G.WS_A]
