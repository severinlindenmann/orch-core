"""Opus delta review of C6b (core#1035, l2-serve): a ``ticket`` item shared by a plain ticket rule must not let the hub
build a decision on a question whose text the owner never shared. ``xfail(strict)`` = the gap is open."""

from __future__ import annotations

import pytest

from orch.hubclient import shares as SH

from .test_share_f1 import UID, _ticket


@pytest.mark.xfail(strict=True, reason="C6b-1: a ticket item's f1 names the owner's open questions (text not shared)")
def test_ticket_rule_alone_never_makes_a_question_buildable():
    items = {i.logical: i for i in SH.select([{"kind": "ticket", "ticket": "DEMO-1"}], [_ticket()])}
    assert set(items) == {f"ticket:{UID}"}  # no question and no needs-you item: the question text is not shared
    tk = items[f"ticket:{UID}"]
    assert "which?" not in repr(tk.payload)
    # contract 8.6 step 3: f1.questions[Q1] in a live item is all the hub needs to build question.answered on Q1
    assert tk.f1["questions"] == {}


def test_ticket_item_f1_carries_repo_and_ref_the_preview_does_not_show():
    """C6b-3 (P2): the plan (``shown``) covers the payload only; the repo URL and branch ref in f1 reach the hub."""
    items = {i.logical: i for i in SH.select([{"kind": "ticket", "ticket": "DEMO-1"}], [_ticket()])}
    tk = items[f"ticket:{UID}"]
    assert "github.com" not in tk.line() and "github.com" in repr(tk.f1)
