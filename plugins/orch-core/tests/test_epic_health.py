"""The epic page at a glance: buckets, state chip, progress, filter, the nothing-waiting line and the empty epic."""
import re

import pytest

from orch.dashboard.data import epic_health as h


def _card(status="open", what="ready", who="agent", why=""):
    return {"status": status, "move": {"what": what, "who": who, "why": why}}


@pytest.mark.parametrize("card,key", [
    (_card("done", "done", "nobody"), "done"),
    (_card("testing", "verdict", "you"), "you"),
    (_card("open", "approve-requirements", "you"), "you"),
    (_card("testing", "ready", "agent"), "testing"),
    (_card("in-progress", "stale"), "idle"),
    (_card("in-progress", "ready"), "idle"),
    (_card("in-progress", "working"), "working"),
    (_card("open", "ready"), "ready"),
    (_card("backlog", "blocked", "nobody"), "ready"),
])
def test_every_card_is_in_exactly_one_bucket(card, key):
    assert h.bucket(card) == key


def test_progress_numbers_add_up_and_the_legend_lists_only_non_zero_buckets():
    n = h.counts(["done", "done", "you", "idle", "idle", "idle", "ready", "working", "testing"])
    assert sum(n.values()) == 9
    p = h.progress(n)
    assert p["sentence"] == "2 of 9 done · 1 waiting on you · 3 with no progress"
    assert sum(s["n"] for s in p["segments"]) == 9
    assert [s["key"] for s in p["legend"]] == ["you", "idle", "working", "testing", "ready", "done"]
    assert h.progress(h.counts(["done", "done"]))["legend"] == [{"key": "done", "label": "Done", "role": "ok", "n": 2}]
    assert "2 done" in p["aria"] and "3 no progress" in p["aria"]
    assert h.progress(h.counts([])) is None


@pytest.mark.parametrize("kw,want", [
    (dict(total=3, n=h.counts(["ready"] * 3), approved=True, factory_state=None, needs=0), ("ok", "On track")),
    (dict(total=3, n=h.counts(["ready"] * 3), approved=True, factory_state=None, needs=2), ("you", "Needs you (2)")),
    (dict(total=3, n=h.counts(["ready"] * 3), approved=False, factory_state=None, needs=1), ("you", "Not approved")),
    (dict(total=3, n=h.counts(["ready"] * 3), approved=True, factory_state="paused", needs=1), ("neu", "Paused")),
    (dict(total=3, n=h.counts(["ready"] * 3), approved=True, factory_state="budget used up", needs=0), ("you", "Stopped")),
    (dict(total=3, n=h.counts(["ready"] * 3), approved=True, factory_state="suspended", needs=0), ("you", "Stopped")),
    (dict(total=3, n=h.counts(["idle", "idle", "done"]), approved=True, factory_state="running", needs=0),
     ("warn", "Stuck: nothing is running")),
    (dict(total=3, n=h.counts(["idle", "working", "done"]), approved=True, factory_state=None, needs=0), ("ok", "On track")),
    (dict(total=0, n=h.counts([]), approved=True, factory_state=None, needs=0), ("neu", "No tickets yet")),
])
def test_state_chip(kw, want):
    assert h.state_chip(**kw) == want


def test_filter_counts_disable_empty_choices_and_fall_back_to_all():
    n = h.counts(["you", "idle", "idle", "ready"])
    f = h.filter_view(n, "idle")
    assert [(o["label"], o["n"], o["disabled"]) for o in f["options"]] == [("All", 4, False), ("Needs you", 1, False),
                                                                           ("No progress", 2, False)]
    assert f["show"] == "idle"
    none = h.filter_view(h.counts(["ready"]), "you")
    assert none["show"] == "all" and [o["disabled"] for o in none["options"]] == [False, True, True]
    assert h.filter_view(n, "bogus")["show"] == "all"


def test_groups_say_a_shared_sub_line_once_and_follow_the_filter():
    rows = [{"bucket": "idle", "sub": "Claimed, but the agent has been silent.", "id": i} for i in range(2)]
    rows.append({"bucket": "ready", "sub": "", "id": 9})
    g = h.groups(rows, "all")
    assert [x["key"] for x in g] == ["idle", "ready"]
    assert g[0]["note"] and all(r["sub"] == "" for r in g[0]["rows"]) and g[0]["n"] == 2
    assert [x["key"] for x in h.groups(rows, "idle")] == ["idle"]


def test_nothing_waiting_only_when_every_source_is_empty():
    base = dict(gate=False, asks=0, permits=0, stopped=False, ready=False, needs_you=0, move_human=False)
    assert h.nothing_waiting(**base)
    for k, v in (("gate", True), ("asks", 1), ("permits", 1), ("stopped", True), ("ready", True), ("needs_you", 2),
                 ("move_human", True)):
        assert not h.nothing_waiting(**{**base, k: v}), k


# -- the rendered page ----------------------------------------------------------------------------------------------

def _refine(ops, tid):
    ops.set_section(tid, "Requirements", "r")
    ops.set_section(tid, "Acceptance criteria", "- [ ] a")
    return tid


def test_empty_epic_page_says_so_and_has_no_bar_or_filter(dash, aops, hops):
    e = aops.new("Empty one", type="epic")
    _refine(aops, e.id)
    hops.approve(e.id, "requirements")
    page = dash.get(f"/t/{e.id}").text
    assert "No tickets yet. They appear here when an agent plans the epic." in page
    assert "No tickets yet" in page.split("tc-chips", 1)[1].split("</p>", 1)[0]  # the chip
    assert 'class="ec-bar"' not in page and 'class="ec-filter"' not in page and "ec-legend" not in page.split("</style>")[-1]
    assert "Nothing is waiting on you." in page


def test_epic_page_shows_progress_filter_and_the_nothing_waiting_line(dash, aops, hops):
    e = aops.new("Billing", type="epic")
    _refine(aops, e.id)
    a, b = (_refine(aops, aops.new(x, epic=e.id).id) for x in ("One", "Two"))
    page = dash.get(f"/t/{e.id}").text
    assert "Nothing is waiting on you." not in page  # the epic itself is not approved yet
    assert "Not approved" in page.split("tc-chips", 1)[1].split("</p>", 1)[0]
    hops.approve(e.id, "requirements", delegate={})
    page = dash.get(f"/t/{e.id}").text
    assert "Nothing is waiting on you." in page
    assert "0 of 2 done" in page
    assert re.search(r'class="ec-bar" role="img" aria-label="0 of 2 done[^"]*2 ready"', page)
    assert ">All 2</a>" in page and 'aria-disabled="true">Needs you 0' in page and 'aria-disabled="true">No progress 0' in page
    assert f"{a}: " in page and f"{b}: " in page
    assert "On track" in page.split("tc-chips", 1)[1].split("</p>", 1)[0]
    # a claimed child with a live agent is "working"; the epic then wants a re-approval, which is the human's move
    aops.claim(a)
    page = dash.get(f"/t/{e.id}").text
    assert re.search(r'ec-g-working">.*?Working <span class="count">1</span>', page, re.S)
    assert "Needs you (1)" in page.split("tc-chips", 1)[1].split("</p>", 1)[0]
    assert "Nothing is waiting on you." not in page
