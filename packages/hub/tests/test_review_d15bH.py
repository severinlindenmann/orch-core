"""Adversarial tests from the Opus conformance review of orch-hub#47 (D15b hub tasks 1-4), tag l2-d15bH."""

from __future__ import annotations

import json

import pytest
from custodial_helpers import LOG, WK, WS
from test_view_service import accept, code, make_seals
from view_helpers import Host, services

from orch_hub.view.service import Keyring


@pytest.fixture
def seals(tmp_path):
    s = make_seals(tmp_path, WS, {1: WK})
    yield s
    s.close()


@pytest.fixture
def svc(tmp_path, seals):
    s = services(tmp_path, seals)
    yield s
    s.close()


def _host():
    h = Host()
    h.genesis()
    h.ticket()
    return h


def test_replayed_older_P_at_the_same_ticket_head_does_not_regress(svc, seals):
    """Attack 2: an old one-line overlap page (P at an older workspace position) replayed after a newer one."""
    h = _host()
    accept(svc, h.page("workspace"))
    old = h.page(LOG, proj=h.proj(status="backlog"))
    accept(svc, old)
    h.append("workspace", "log.added", text="workspace moved on")  # not a roster type: roster_v stays
    accept(svc, h.page("workspace", len(h.logs["workspace"]), 1))
    accept(svc, h.page(LOG, 1, 1, proj=h.proj(status="open")))
    accept(svc, old)  # the replay is a valid page; its P is older by (seq, ws_seq)
    p = svc.service.proj(WS, LOG, Keyring(seals, WS, {1: WK}))
    assert (p["status"], p["ws_seq"]) == ("open", len(h.logs["workspace"]))


def _raw(b):
    return len(json.dumps(b, sort_keys=True, separators=(",", ":")))


def test_cap_counts_the_projection_row_of_the_page_log(svc, monkeypatch):
    from orch_hub.view import store as ST

    h = _host()
    accept(svc, h.page("workspace"))
    accept(svc, h.page(LOG, proj=h.proj()))
    held = sum(_raw(b) for lg in svc.store.logs(WS) for b in svc.store.pages(WS, lg))
    row = _raw(svc.store.proj_page(WS, LOG))
    h.ask()
    nxt = h.page(LOG, 2)  # new lines, no proj: only the page is added, the held P row stays
    monkeypatch.setattr(ST, "MAX_WS_BYTES", held + row + _raw(nxt) - 1)  # one byte short of what would be kept
    assert code(accept, svc, nxt) == "too_large"
