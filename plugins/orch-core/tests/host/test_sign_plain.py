"""B6 (owner decision E): the signing review in three layers: a headline, the content formatted like the ticket
page, the prompt; the full dump behind ``d`` / ``--details``. Same signed material; only the layout changes."""

from __future__ import annotations

import os
import pty
import threading
import time

import pytest

from orch.host import signcontent, signer
from orch.ops import human as ops_human
from tests.host import human_helpers
from tests.host.test_human import ready

lab, served, human = human_helpers.lab, human_helpers.served, human_helpers.human

WS = lambda w: ("Acme", "DEMO")  # noqa: E731

APPROVE = """\
Approve the requirements of DEMO-0001
in workspace Acme (DEMO)

Context
  the context

Out of scope
  the out_of_scope

Requirements
  the requirements

Summary
  (empty)

Acceptance criteria
  [ ] AC1  it works

Fields
  type: feature
"""

NEW = """\
Create ticket DEMO-0002 · Elefant Chart
in workspace Acme (DEMO)

Fields
  type: chore
"""


def test_the_plain_view_of_an_approval_is_headline_and_content_without_hashes(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    text = signer.plain(p, WS)
    assert str(text) == APPROVE
    for hidden in ("sha256", "fingerprint", "uid ", "pid ", "not proven", "self-declared", "policy", "gate input"):
        assert hidden not in text
    assert "fingerprint: " + p["fingerprint"] in text.details and "gate input:" in text.details
    assert "ticket key per host, not proven" in text.details and "self-declared, unverified" in text.details


def test_the_plain_view_of_a_new_ticket(human, served):
    _, key = human.ticket("Owner CLI test")
    p = human.prepare("new", {"title": "Elefant Chart", "type": "chore"})
    assert str(signer.plain(p, WS)) == NEW


def test_the_content_comes_from_the_signed_events_and_material_only(human, served):
    _, key = human.ticket("Owner CLI test")
    ready(human, key)
    p = human.prepare("approve", {"ref": key, "gate": "requirements"})
    c = signcontent.build(p)
    assert c["headline"] == "Approve the requirements of DEMO-0001"
    assert c["ticket"] == {"key": "DEMO-0001", "title": ""}  # a gate does not bind the title
    assert c["acceptance"] == [{"id": "AC1", "text": "it works", "checked": False}]
    assert {s["id"]: s["text"] for s in c["sections"]}["requirements"] == "the requirements"
    # a tampered text is no longer what the events sign: the signer refuses before it renders
    bad = {**p, "material": {**p["material"], "texts": {**p["material"]["texts"], "requirements": "other"}}}
    with pytest.raises(signer.SignerRefused):
        human.check(bad)


def test_changed_fields_show_before_and_after_and_tasks_are_numbered(human, served):
    _, key = human.ticket("Owner CLI test")
    p = human.prepare("set", {"ref": key, "pairs": ["priority=high"]})
    c = signcontent.build(p)
    assert c["fields_changed"] == [{"field": "priority", "before": "medium", "after": "high"}]
    assert c["headline"] == "Change the priority of DEMO-0001"
    plan = {  # the plan gate binds the tasks with their verify commands
        "op": "approve",
        "events": [{"type": "gate.approved"}],
        "material": {
            "kind": "gate",
            "gate": "plan",
            "ticket": {"key": "DEMO-0001", "uid": "u"},
            "texts": {},
            "input": {
                "fields": {},
                "tasks": [
                    {"id": "T1", "text": "write it", "verify": {"cmd": "pytest -q"}},
                    {"id": "T2", "text": "ship it", "verify": None},
                ],
            },
        },
    }
    c = signcontent.build(plan)
    assert c["headline"] == "Approve the plan of DEMO-0001"
    assert c["tasks"] == [{"n": 1, "title": "write it", "verify": ["pytest -q"]}, {"n": 2, "title": "ship it"}]
    assert "  1. write it\n       verify: pytest -q\n  2. ship it\n" in "\n".join(signcontent.text(c, signer._clean))


def test_a_control_character_never_reaches_the_plain_view(human, served):
    _, key = human.ticket("Owner CLI test")
    p = human.prepare("log", {"ref": key, "text": "line one\nline two"})
    text = str(signer.plain(p, WS))
    assert "  line one\n  line two\n" in text
    esc = {**p, "events": [{**p["events"][0], "text": "x\x1b[2Jy"}]}
    assert "\x1b" not in str(signcontent.text(signcontent.build(esc), signer._clean))


def test_the_view_of_a_click_carries_the_headline_and_the_content_next_to_the_unchanged_lines(human, served):
    from orch.click import view

    _, key = human.ticket("Owner CLI test")
    p = human.prepare("log", {"ref": key, "text": "note"})
    v = view.build(
        p, WS, kind="click", device=p["events"][0]["actor"]["device"], expires_at="2026-01-01T00:00:00Z", artifacts=None
    )
    assert v["headline"] == "Add a note to DEMO-0001" and v["content"]["sections"][0]["text"] == "note"
    assert v["lines"] == signer.render(p, WS).rstrip("\n").split("\n")  # the full review is still there, line for line
    assert v["risk"]


# --- the prompt -------------------------------------------------------------------------------------------------------


def _prompt(monkeypatch, text, answers):
    """Run ``review_prompt`` on a pty; the test types ``answers``, one after each prompt. Returns (result, shown)."""
    master, slave = pty.openpty()
    monkeypatch.setattr(ops_human, "open_tty", lambda flags: os.dup(slave))
    shown = bytearray()
    done = threading.Event()

    def person():
        queue = list(answers)
        seen = 0
        while queue and not done.is_set():
            try:
                shown.extend(os.read(master, 65536))
            except OSError:
                return
            prompts = shown.count(b"): ") + shown.count(b"stops: ")
            if prompts > seen:
                seen = prompts
                time.sleep(0.3)  # the prompt flushes pending input once it is written: type after that
                os.write(master, queue.pop(0).encode() + b"\n")

    t = threading.Thread(target=person, daemon=True)
    t.start()
    watchdog = threading.Timer(8, lambda: os.write(master, b"\n"))  # never hang a test run
    watchdog.start()
    try:
        result = ops_human.review_prompt(text, "DEMO-0001/ABCD1234")
    finally:
        watchdog.cancel()
        done.set()
        os.close(slave)
        t.join(2)
        os.close(master)
    return result, shown.decode()


def _review():
    r = signer.Review("Approve the requirements of DEMO-0001\n\n")
    r.details = "FULL DUMP\nfingerprint: abc\n"
    return r


def test_the_prompt_shows_the_plain_view_and_d_prints_the_full_dump(monkeypatch):
    ok, shown = _prompt(monkeypatch, _review(), ["d", "DEMO-0001/ABCD1234"])
    assert ok and "Type DEMO-0001/ABCD1234 to continue (d = details): " in shown
    assert shown.index("Approve the requirements") < shown.index("FULL DUMP")  # plain first, details on request


def test_without_d_the_details_are_not_shown(monkeypatch):
    ok, shown = _prompt(monkeypatch, _review(), ["DEMO-0001/ABCD1234"])
    assert ok and "FULL DUMP" not in shown


def test_a_wrong_word_stops(monkeypatch):
    ok, _ = _prompt(monkeypatch, _review(), ["d", "yes"])
    assert not ok


def test_details_first_shows_the_dump_without_d(monkeypatch):
    r = _review()
    r.details_first = True
    ok, shown = _prompt(monkeypatch, r, ["DEMO-0001/ABCD1234"])
    assert ok and "FULL DUMP" in shown and "Approve the requirements" not in shown


def test_a_review_without_details_keeps_the_old_prompt(monkeypatch):
    ok, shown = _prompt(monkeypatch, "plain text\n", ["DEMO-0001/ABCD1234"])
    assert ok and "and Enter to continue to the passphrase; anything else stops: " in shown

