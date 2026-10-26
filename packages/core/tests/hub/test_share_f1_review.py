"""Opus delta review of C6b (core#1035, l2-serve): a ``ticket`` item shared by a plain ticket rule must not let the hub
build a decision on a question whose text the owner never shared. ``xfail(strict)`` = the gap is open."""

from __future__ import annotations

from orch.hubclient import shares as SH

from .test_share_f1 import SRC, UID, _gate, _ticket


def test_ticket_rule_alone_never_makes_a_question_buildable():
    items = {i.logical: i for i in SH.select([{"kind": "ticket", "ticket": "DEMO-1"}], [_ticket()])}
    assert set(items) == {f"ticket:{UID}"}  # no question and no needs-you item: the question text is not shared
    tk = items[f"ticket:{UID}"]
    assert "which?" not in repr(tk.payload)
    # contract 8.6 step 3: f1.questions[Q1] in a live item is all the hub needs to build question.answered on Q1
    assert tk.f1 is None  # no f1 at all: a decision is built only from an item that names it (C6b-1, C6b-2)


def test_repos_and_refs_in_f1_are_in_the_preview_and_the_plan():
    """C6b-3: a gate summary's f1 carries repo URLs and refs; its preview line shows them and ``shown`` covers them."""
    t = _ticket()
    rule = [{"kind": "needs_you"}]
    (item,) = [i for i in SH.select(rule, [t]) if i.logical.endswith("approve:code")]
    assert "github.com/demo/api" in item.line() and "refs/heads/t-1" in item.line()
    moved = _ticket(gates=(_gate("plan"), _gate("code", "2", sources=[{**SRC[0], "ref": "refs/heads/other"}])))
    (item2,) = [i for i in SH.select(rule, [moved]) if i.logical.endswith("approve:code")]
    assert item.shown != item2.shown  # a changed repo or ref changes the plan the owner confirms


def test_repo_strings_that_differ_only_in_whitespace_give_different_plans():
    """C6bd-1 (core#1040): the plan hashes the raw (repo, ref, sha) tuples, not the cleaned preview lines."""
    rule = [{"kind": "needs_you"}]
    plans = set()
    lines = set()
    for repo in ("a b", "a  b", "a   b"):
        t = _ticket(gates=(_gate("plan"), _gate("code", "2", sources=[{**SRC[0], "repo": repo}])))
        (item,) = [i for i in SH.select(rule, [t]) if i.logical.endswith("approve:code")]
        plans.add(SH.plan_of([item]))
        lines.add(item.line())
    assert len(plans) == 3
    assert len(lines) == 1  # the preview text stays cleaned (the lines may collide, the plans must not)
